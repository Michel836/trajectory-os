"""M057 — classical predictive ML baseline over the M056 learning dataset.

No third-party ML dependency is introduced. Every algorithm is a small,
deterministic, auditable pure-Python implementation:

* :class:`RidgeRegression`     — closed-form ridge via normal equations;
* :class:`LogisticRegression`  — L2-regularised batch gradient descent;
* :class:`DecisionTreeRegressor` / :class:`DecisionTreeClassifier` — CART;
* :class:`RandomForest`        — deterministic bagging + feature subsampling;
* :class:`GradientBoostingRegressor` — shallow additive residual trees.

The training protocol is deliberately conservative:

* the M056 train/validation/test split is used *as-is* (no reshuffling), and
  the encoder is fit on the training split only;
* every target is compared against a **naive baseline** (mean for regression,
  majority class for classification);
* a model is only reported ``TRAINED`` when the dataset passes the explicit
  minimum-size and class-balance fail-safe; otherwise ``INSUFFICIENT_DATA``;
* classification probabilities are calibrated on the validation split and the
  before/after calibration is reported honestly;
* no statistical significance is fabricated: only point metrics and an
  explicitly-derived uncertainty are persisted.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from trajectory_os.intelligence import dataset, features, model

#: Evaluation statuses (closed set).
ST_TRAINED = "TRAINED"
ST_INSUFFICIENT = "INSUFFICIENT_DATA"

#: Algorithm identifiers (closed set, deterministic selection order).
ALG_RIDGE = "ridge"
ALG_LOGISTIC = "logistic"
ALG_FOREST = "random_forest"
ALG_BOOSTING = "gradient_boosting"
ALG_DECISION_TREE = "decision_tree"
ALG_MEAN = "naive_mean"
ALG_MAJORITY = "naive_majority"

REGRESSION_ALGORITHMS = (ALG_RIDGE, ALG_FOREST, ALG_BOOSTING)
CLASSIFICATION_ALGORITHMS = (ALG_LOGISTIC, ALG_FOREST)

#: Minimum rows per split for a trainable target.
MIN_TRAIN_ROWS = 10
MIN_VALIDATION_ROWS = 4
MIN_TEST_ROWS = 3
MIN_PER_CLASS = 4

#: Default hyperparameters (explicit and persisted).
RIDGE_ALPHA = 1.0
LOGISTIC_RATE = 0.3
LOGISTIC_EPOCHS = 400
LOGISTIC_L2 = 0.01
TREE_MAX_DEPTH = 3
TREE_MIN_LEAF = 2
FOREST_TREES = 16
FOREST_MAX_DEPTH = 4
FOREST_MAX_FEATURES = 0.7
BOOST_TREES = 24
BOOST_MAX_DEPTH = 2
BOOST_RATE = 0.1

#: Deterministic bagging seed (never a clock).
FOREST_SEED = 20240507


# --- linear algebra helpers ---------------------------------------------------


Matrix = list[list[float]]


def _solve_linear(matrix: Matrix, rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (deterministic)."""
    n = len(matrix)
    augmented = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            continue
        augmented[column], augmented[pivot] = (
            augmented[pivot], augmented[column])
        pivot_value = augmented[column][column]
        for row in range(column + 1, n):
            factor = augmented[row][column] / pivot_value
            if factor == 0.0:
                continue
            for k in range(column, n + 1):
                augmented[row][k] -= factor * augmented[column][k]
    solution = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = augmented[row][n]
        for k in range(row + 1, n):
            total -= augmented[row][k] * solution[k]
        diagonal = augmented[row][row]
        solution[row] = total / diagonal if abs(diagonal) > 1e-12 else 0.0
    return solution


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


class _Standardizer:
    """Train-only feature standardization (never fits on eval data)."""

    def __init__(self, means: Sequence[float], scales: Sequence[float]) -> None:
        self.means = list(means)
        self.scales = [scale if scale > 1e-12 else 1.0 for scale in scales]

    @staticmethod
    def fit(matrix: Matrix) -> _Standardizer:
        if not matrix:
            return _Standardizer([], [])
        width = len(matrix[0])
        count = float(len(matrix))
        means = [0.0] * width
        for row in matrix:
            for index, value in enumerate(row):
                means[index] += value
        means = [mean / count for mean in means]
        variances = [0.0] * width
        for row in matrix:
            for index, value in enumerate(row):
                variances[index] += (value - means[index]) ** 2
        scales = [math.sqrt(var / count) for var in variances]
        return _Standardizer(means, scales)

    def transform(self, matrix: Matrix) -> Matrix:
        return [
            [(value - self.means[i]) / self.scales[i]
             for i, value in enumerate(row)]
            for row in matrix
        ]

    def transform_row(self, row: Sequence[float]) -> list[float]:
        return [(value - self.means[i]) / self.scales[i]
                for i, value in enumerate(row)]

    def to_dict(self) -> dict[str, Any]:
        return {"means": list(self.means), "scales": list(self.scales)}

    @staticmethod
    def from_dict(document: Mapping[str, Any]) -> _Standardizer:
        means = [float(v) for v in model.require_list(
            document.get("means", []), "means")]
        scales = [float(v) for v in model.require_list(
            document.get("scales", []), "scales")]
        return _Standardizer(means, scales)


# --- regressors ---------------------------------------------------------------


class Regressor(Protocol):
    name: str

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None: ...

    def predict(self, matrix: Matrix) -> list[float]: ...

    def importances(self, dimension: int) -> list[float]: ...

    def to_dict(self) -> dict[str, Any]: ...


class RidgeRegression:
    """Ridge regression via the normal equations (deterministic)."""

    name = ALG_RIDGE

    def __init__(self, alpha: float = RIDGE_ALPHA) -> None:
        self.alpha = alpha
        self.standardizer: _Standardizer | None = None
        self.coefficients: list[float] = []
        self.intercept: float = 0.0

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None:
        self.standardizer = _Standardizer.fit(matrix)
        scaled = self.standardizer.transform(matrix)
        width = len(scaled[0]) if scaled else 0
        dim = width + 1
        xtx = [[0.0] * dim for _ in range(dim)]
        xty = [0.0] * dim
        for row, y in zip(scaled, target, strict=True):
            extended = [1.0, *row]
            for i in range(dim):
                xty[i] += extended[i] * y
                for j in range(dim):
                    xtx[i][j] += extended[i] * extended[j]
        for i in range(1, dim):
            xtx[i][i] += self.alpha
        solution = _solve_linear(xtx, xty)
        self.intercept = solution[0]
        self.coefficients = solution[1:]

    def predict(self, matrix: Matrix) -> list[float]:
        assert self.standardizer is not None, "fit before predict"
        out: list[float] = []
        for row in matrix:
            scaled = self.standardizer.transform_row(row)
            out.append(self.intercept + sum(
                c * v for c, v in zip(self.coefficients, scaled,
                                      strict=True)))
        return out

    def importances(self, dimension: int) -> list[float]:
        magnitudes = [abs(value) for value in self.coefficients]
        total = sum(magnitudes)
        if total <= 1e-12:
            return [0.0] * dimension
        return [value / total for value in magnitudes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.name,
            "hyperparameters": {"alpha": self.alpha},
            "standardizer": (self.standardizer.to_dict()
                             if self.standardizer is not None else None),
            "intercept": self.intercept,
            "coefficients": list(self.coefficients),
        }


# --- trees --------------------------------------------------------------------


@dataclass(frozen=True)
class TreeNode:
    """One CART node (leaf when ``feature`` is ``None``)."""

    feature: int | None
    threshold: float
    left: TreeNode | None
    right: TreeNode | None
    values: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "threshold": self.threshold,
            "left": self.left.to_dict() if self.left is not None else None,
            "right": self.right.to_dict() if self.right is not None else None,
            "values": list(self.values),
        }

    @staticmethod
    def from_dict(document: Mapping[str, Any]) -> TreeNode:
        left = document.get("left")
        right = document.get("right")
        feature = document.get("feature")
        return TreeNode(
            feature=(int(feature) if feature is not None else None),
            threshold=float(document.get("threshold", 0.0)),
            left=TreeNode.from_dict(model.require_mapping(left, "left"))
            if left is not None else None,
            right=TreeNode.from_dict(model.require_mapping(right, "right"))
            if right is not None else None,
            values=tuple(float(v) for v in model.require_list(
                document.get("values", []), "values")),
        )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _gini(labels: Sequence[float]) -> float:
    if not labels:
        return 0.0
    positives = sum(1 for value in labels if value >= 0.5)
    p = positives / len(labels)
    return 2.0 * p * (1.0 - p)


def _variance(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    return sum((value - mean) ** 2 for value in values)


def _best_split(
    matrix: Matrix,
    labels: Sequence[float],
    indices: Sequence[int],
    candidate_features: Sequence[int],
    *,
    classification: bool,
    max_thresholds: int = 16,
) -> tuple[int | None, float, float]:
    """Return (feature, threshold, gain) for the best candidate split."""
    best_feature: int | None = None
    best_threshold = 0.0
    best_gain = 0.0
    base = (_gini(labels) if classification else _variance(labels))
    for feature in candidate_features:
        values = sorted({matrix[i][feature] for i in indices})
        if len(values) < 2:
            continue
        thresholds = _candidate_thresholds(values, max_thresholds)
        for threshold in thresholds:
            left = [i for i in indices if matrix[i][feature] <= threshold]
            right = [i for i in indices if matrix[i][feature] > threshold]
            if not left or not right:
                continue
            left_labels = [labels[i] for i in left]
            right_labels = [labels[i] for i in right]
            weighted = (
                len(left) * _gini(left_labels)
                + len(right) * _gini(right_labels))
            total = (
                len(left) * _variance(left_labels)
                + len(right) * _variance(right_labels))
            gain = base - (weighted / len(indices) if classification
                           else total / len(indices))
            if gain > best_gain + 1e-12:
                best_gain = gain
                best_feature = feature
                best_threshold = threshold
    return best_feature, best_threshold, best_gain


def _candidate_thresholds(values: Sequence[float],
                          maximum: int) -> list[float]:
    if len(values) - 1 <= maximum:
        return [(values[i] + values[i + 1]) / 2.0
                for i in range(len(values) - 1)]
    step = (len(values) - 1) / (maximum + 1)
    thresholds: list[float] = []
    for slot in range(1, maximum + 1):
        index = int(round(slot * step))
        index = max(0, min(len(values) - 2, index))
        thresholds.append((values[index] + values[index + 1]) / 2.0)
    return sorted(set(thresholds))


def _build_tree(
    matrix: Matrix,
    labels: Sequence[float],
    indices: Sequence[int],
    *,
    depth: int,
    max_depth: int,
    min_leaf: int,
    classification: bool,
    feature_sampler: Callable[[], Sequence[int]],
    importances: list[float],
) -> TreeNode:
    leaf_values = _leaf_values(labels, indices, classification)
    if depth >= max_depth or len(indices) < 2 * min_leaf:
        return TreeNode(None, 0.0, None, None, leaf_values)
    if classification and len({labels[i] for i in indices}) == 1:
        return TreeNode(None, 0.0, None, None, leaf_values)
    if not classification and _variance([labels[i] for i in indices]) < 1e-12:
        return TreeNode(None, 0.0, None, None, leaf_values)
    candidates = feature_sampler()
    feature, threshold, gain = _best_split(
        matrix, labels, indices, candidates, classification=classification)
    if feature is None or gain <= 1e-12:
        return TreeNode(None, 0.0, None, None, leaf_values)
    left_indices = [i for i in indices if matrix[i][feature] <= threshold]
    right_indices = [i for i in indices if matrix[i][feature] > threshold]
    importances[feature] += gain * len(indices)
    left = _build_tree(
        matrix, labels, left_indices, depth=depth + 1,
        max_depth=max_depth, min_leaf=min_leaf, classification=classification,
        feature_sampler=feature_sampler, importances=importances)
    right = _build_tree(
        matrix, labels, right_indices, depth=depth + 1,
        max_depth=max_depth, min_leaf=min_leaf, classification=classification,
        feature_sampler=feature_sampler, importances=importances)
    return TreeNode(feature, threshold, left, right, leaf_values)


def _leaf_values(labels: Sequence[float], indices: Sequence[int],
                 classification: bool) -> tuple[float, ...]:
    if classification:
        positives = sum(1 for i in indices if labels[i] >= 0.5)
        probability = (positives + 1.0) / (len(indices) + 2.0)
        return (probability,)
    return (_mean([labels[i] for i in indices]),)


def _predict_tree(node: TreeNode, row: Sequence[float]) -> tuple[float, ...]:
    current = node
    while current.feature is not None:
        assert current.left is not None and current.right is not None
        current = (current.left if row[current.feature] <= current.threshold
                   else current.right)
    return current.values


class DecisionTreeRegressor:
    """Deterministic CART regression tree."""

    name = ALG_DECISION_TREE

    def __init__(self, *, max_depth: int = TREE_MAX_DEPTH,
                 min_leaf: int = TREE_MIN_LEAF, seed: int = 0,
                 max_features: int | None = None) -> None:
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.seed = seed
        self.max_features = max_features
        self.root: TreeNode | None = None
        self._importances: list[float] = []
        self._dimension = 0

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None:
        self._dimension = len(matrix[0]) if matrix else 0
        self._importances = [0.0] * self._dimension
        rng = random.Random(self.seed)

        def sampler() -> Sequence[int]:
            return _sample_features(self._dimension, self.max_features, rng)

        self.root = _build_tree(
            matrix, list(target), list(range(len(matrix))), depth=0,
            max_depth=self.max_depth, min_leaf=self.min_leaf,
            classification=False, feature_sampler=sampler,
            importances=self._importances)

    def predict(self, matrix: Matrix) -> list[float]:
        assert self.root is not None, "fit before predict"
        return [_predict_tree(self.root, row)[0] for row in matrix]

    def importances(self, dimension: int) -> list[float]:
        total = sum(self._importances)
        if total <= 1e-12:
            return [0.0] * dimension
        return [value / total for value in self._importances]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": "decision_tree",
            "hyperparameters": {"max_depth": self.max_depth,
                                "min_leaf": self.min_leaf},
            "root": self.root.to_dict() if self.root is not None else None,
            "importances": list(self._importances),
        }


def _sample_features(dimension: int, maximum: int | None,
                     rng: random.Random) -> Sequence[int]:
    if maximum is None or maximum >= dimension:
        return list(range(dimension))
    count = max(1, min(dimension, maximum))
    return sorted(rng.sample(range(dimension), count))


class DecisionTreeClassifier:
    """Deterministic binary CART classifier (predicts positive-class prob)."""

    name = ALG_DECISION_TREE
    def __init__(self, *, max_depth: int = TREE_MAX_DEPTH,
                 min_leaf: int = TREE_MIN_LEAF, seed: int = 0,
                 max_features: int | None = None) -> None:
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.seed = seed
        self.max_features = max_features
        self.root: TreeNode | None = None
        self._importances: list[float] = []
        self._dimension = 0

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None:
        self._dimension = len(matrix[0]) if matrix else 0
        self._importances = [0.0] * self._dimension
        rng = random.Random(self.seed)

        def sampler() -> Sequence[int]:
            return _sample_features(self._dimension, self.max_features, rng)

        self.root = _build_tree(
            matrix, list(target), list(range(len(matrix))), depth=0,
            max_depth=self.max_depth, min_leaf=self.min_leaf,
            classification=True, feature_sampler=sampler,
            importances=self._importances)

    def predict_proba(self, matrix: Matrix) -> list[float]:
        assert self.root is not None, "fit before predict"
        return [_predict_tree(self.root, row)[0] for row in matrix]

    def importances(self, dimension: int) -> list[float]:
        total = sum(self._importances)
        if total <= 1e-12:
            return [0.0] * dimension
        return [value / total for value in self._importances]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": "decision_tree",
            "hyperparameters": {"max_depth": self.max_depth,
                                "min_leaf": self.min_leaf},
            "root": self.root.to_dict() if self.root is not None else None,
            "importances": list(self._importances),
        }


# --- ensembles ----------------------------------------------------------------


class RandomForest:
    """Deterministic random forest (regression or binary classification)."""

    def __init__(self, *, classification: bool, trees: int = FOREST_TREES,
                 max_depth: int = FOREST_MAX_DEPTH,
                 min_leaf: int = TREE_MIN_LEAF,
                 max_feature_fraction: float = FOREST_MAX_FEATURES,
                 seed: int = FOREST_SEED) -> None:
        self.classification = classification
        self.trees = trees
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.max_feature_fraction = max_feature_fraction
        self.seed = seed
        self.estimators: list[TreeNode] = []
        self._importances: list[float] = []
        self._dimension = 0
        self.name = ALG_FOREST

    def _new_estimator(self, index: int) -> DecisionTreeRegressor | (
            DecisionTreeClassifier):
        dimension = self._dimension
        maximum = max(1, int(round(dimension * self.max_feature_fraction)))
        if self.classification:
            return DecisionTreeClassifier(
                max_depth=self.max_depth, min_leaf=self.min_leaf,
                seed=self.seed + index, max_features=maximum)
        return DecisionTreeRegressor(
            max_depth=self.max_depth, min_leaf=self.min_leaf,
            seed=self.seed + index, max_features=maximum)

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None:
        self._dimension = len(matrix[0]) if matrix else 0
        self._importances = [0.0] * self._dimension
        self.estimators = []
        rng = random.Random(self.seed)
        count = len(matrix)
        for index in range(self.trees):
            estimator = self._new_estimator(index)
            indices = [rng.randrange(count) for _ in range(count)]
            bootstrap = [matrix[i] for i in indices]
            labels = [target[i] for i in indices]
            if self.classification:
                assert isinstance(estimator, DecisionTreeClassifier)
                estimator.fit(bootstrap, labels)
                assert estimator.root is not None
                self.estimators.append(estimator.root)
            else:
                assert isinstance(estimator, DecisionTreeRegressor)
                estimator.fit(bootstrap, labels)
                assert estimator.root is not None
                self.estimators.append(estimator.root)
            for dim, value in enumerate(
                    estimator.importances(self._dimension)):
                self._importances[dim] += value

    def _tree_predictions(self, matrix: Matrix) -> list[list[float]]:
        out: list[list[float]] = []
        for node in self.estimators:
            out.append([_predict_tree(node, row)[0] for row in matrix])
        return out

    def predict(self, matrix: Matrix) -> list[float]:
        predictions = self._tree_predictions(matrix)
        if not predictions:
            return [0.0] * len(matrix)
        return [
            _mean([tree[row_index] for tree in predictions])
            for row_index in range(len(matrix))
        ]

    def predict_proba(self, matrix: Matrix) -> list[float]:
        return self.predict(matrix)

    def importances(self, dimension: int) -> list[float]:
        if not self.estimators:
            return [0.0] * dimension
        total = sum(self._importances)
        if total <= 1e-12:
            return [0.0] * dimension
        return [value / total for value in self._importances]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.name,
            "hyperparameters": {
                "classification": self.classification,
                "trees": self.trees,
                "max_depth": self.max_depth,
                "min_leaf": self.min_leaf,
                "max_feature_fraction": self.max_feature_fraction,
                "seed": self.seed,
            },
            "estimators": [node.to_dict() for node in self.estimators],
            "importances": list(self._importances),
        }


class GradientBoostingRegressor:
    """Shallow additive gradient boosting for regression."""

    name = ALG_BOOSTING

    def __init__(self, *, trees: int = BOOST_TREES,
                 max_depth: int = BOOST_MAX_DEPTH, rate: float = BOOST_RATE,
                 min_leaf: int = TREE_MIN_LEAF) -> None:
        self.trees = trees
        self.max_depth = max_depth
        self.rate = rate
        self.min_leaf = min_leaf
        self.base: float = 0.0
        self.estimators: list[TreeNode] = []
        self._importances: list[float] = []
        self._dimension = 0

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None:
        self._dimension = len(matrix[0]) if matrix else 0
        self._importances = [0.0] * self._dimension
        self.base = _mean(target)
        residuals = [value - self.base for value in target]
        self.estimators = []
        for index in range(self.trees):
            tree = DecisionTreeRegressor(
                max_depth=self.max_depth, min_leaf=self.min_leaf,
                seed=index)
            tree.fit(matrix, residuals)
            assert tree.root is not None
            self.estimators.append(tree.root)
            predictions = tree.predict(matrix)
            residuals = [
                residual - self.rate * prediction
                for residual, prediction in zip(
                    residuals, predictions, strict=True)]
            for dim, value in enumerate(
                    tree.importances(self._dimension)):
                self._importances[dim] += value

    def predict(self, matrix: Matrix) -> list[float]:
        out = [self.base] * len(matrix)
        for node in self.estimators:
            for row_index, row in enumerate(matrix):
                out[row_index] += self.rate * _predict_tree(node, row)[0]
        return out

    def importances(self, dimension: int) -> list[float]:
        total = sum(self._importances)
        if total <= 1e-12:
            return [0.0] * dimension
        return [value / total for value in self._importances]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.name,
            "hyperparameters": {"trees": self.trees,
                                "max_depth": self.max_depth,
                                "rate": self.rate},
            "base": self.base,
            "estimators": [node.to_dict() for node in self.estimators],
            "importances": list(self._importances),
        }


class LogisticRegression:
    """L2-regularised logistic regression via batch gradient descent."""

    name = ALG_LOGISTIC

    def __init__(self, *, rate: float = LOGISTIC_RATE,
                 epochs: int = LOGISTIC_EPOCHS,
                 l2: float = LOGISTIC_L2) -> None:
        self.rate = rate
        self.epochs = epochs
        self.l2 = l2
        self.standardizer: _Standardizer | None = None
        self.weights: list[float] = []
        self.intercept: float = 0.0

    def fit(self, matrix: Matrix, target: Sequence[float]) -> None:
        self.standardizer = _Standardizer.fit(matrix)
        scaled = self.standardizer.transform(matrix)
        width = len(scaled[0]) if scaled else 0
        self.weights = [0.0] * width
        self.intercept = 0.0
        count = float(len(scaled))
        if not scaled:
            return
        for _ in range(self.epochs):
            gradient_w = [0.0] * width
            gradient_b = 0.0
            for row, y in zip(scaled, target, strict=True):
                linear = self.intercept + sum(
                    w * v for w, v in zip(self.weights, row, strict=True))
                error = _sigmoid(linear) - y
                gradient_b += error
                for index, value in enumerate(row):
                    gradient_w[index] += error * value
            self.intercept -= self.rate * gradient_b / count
            for index in range(width):
                penalty = self.l2 * self.weights[index]
                self.weights[index] -= self.rate * (
                    gradient_w[index] / count + penalty)

    def predict_proba(self, matrix: Matrix) -> list[float]:
        assert self.standardizer is not None, "fit before predict"
        out: list[float] = []
        for row in matrix:
            scaled = self.standardizer.transform_row(row)
            linear = self.intercept + sum(
                w * v for w, v in zip(self.weights, scaled, strict=True))
            out.append(_sigmoid(linear))
        return out

    def importances(self, dimension: int) -> list[float]:
        magnitudes = [abs(value) for value in self.weights]
        total = sum(magnitudes)
        if total <= 1e-12:
            return [0.0] * dimension
        return [value / total for value in magnitudes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.name,
            "hyperparameters": {"rate": self.rate, "epochs": self.epochs,
                                "l2": self.l2},
            "standardizer": (self.standardizer.to_dict()
                             if self.standardizer is not None else None),
            "weights": list(self.weights),
            "intercept": self.intercept,
        }


# --- metrics ------------------------------------------------------------------


@dataclass(frozen=True)
class RegressionMetrics:
    mae: float
    rmse: float
    r2: float
    mean_prediction: float
    residual_std: float
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"mae": self.mae, "rmse": self.rmse, "r2": self.r2,
                "mean_prediction": self.mean_prediction,
                "residual_std": self.residual_std, "count": self.count}


def regression_metrics(predictions: Sequence[float],
                       actual: Sequence[float]) -> RegressionMetrics:
    if len(predictions) != len(actual) or not actual:
        model.fail(model.E_MALFORMED, "prediction/actual length mismatch")
    residuals = [p - a for p, a in zip(predictions, actual, strict=True)]
    mae = sum(abs(r) for r in residuals) / len(residuals)
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    mean_actual = _mean(actual)
    total = sum((a - mean_actual) ** 2 for a in actual)
    residual_sse = sum(r * r for r in residuals)
    r2 = 1.0 - residual_sse / total if total > 1e-12 else 0.0
    variance = sum(r * r for r in residuals) / max(1, len(residuals) - 1)
    return RegressionMetrics(
        mae=mae, rmse=rmse, r2=r2, mean_prediction=_mean(predictions),
        residual_std=math.sqrt(variance), count=len(actual))


@dataclass(frozen=True)
class ClassificationMetrics:
    accuracy: float
    precision: float
    recall: float
    f1: float
    brier: float
    log_loss: float
    auc: float
    base_rate: float
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"accuracy": self.accuracy, "precision": self.precision,
                "recall": self.recall, "f1": self.f1, "brier": self.brier,
                "log_loss": self.log_loss, "auc": self.auc,
                "base_rate": self.base_rate, "count": self.count}


def classification_metrics(probabilities: Sequence[float],
                           actual: Sequence[float]) -> ClassificationMetrics:
    if len(probabilities) != len(actual) or not actual:
        model.fail(model.E_MALFORMED, "prediction/actual length mismatch")
    clipped = [min(1.0 - 1e-9, max(1e-9, p)) for p in probabilities]
    predictions = [1.0 if p >= 0.5 else 0.0 for p in clipped]
    true_positive = sum(1 for p, a in zip(predictions, actual, strict=True)
                        if p >= 0.5 and a >= 0.5)
    false_positive = sum(1 for p, a in zip(predictions, actual, strict=True)
                         if p >= 0.5 and a < 0.5)
    false_negative = sum(1 for p, a in zip(predictions, actual, strict=True)
                         if p < 0.5 and a >= 0.5)
    accuracy = (sum(1 for p, a in zip(predictions, actual, strict=True)
                    if (p >= 0.5) == (a >= 0.5)) / len(actual))
    precision = (true_positive / (true_positive + false_positive)
                 if true_positive + false_positive else 0.0)
    recall = (true_positive / (true_positive + false_negative)
              if true_positive + false_negative else 0.0)
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall > 0 else 0.0)
    brier = sum((p - a) ** 2 for p, a in zip(clipped, actual, strict=True)
                ) / len(actual)
    log_loss = -sum(
        a * math.log(p) + (1 - a) * math.log(1 - p)
        for p, a in zip(clipped, actual, strict=True)) / len(actual)
    return ClassificationMetrics(
        accuracy=accuracy, precision=precision, recall=recall, f1=f1,
        brier=brier, log_loss=log_loss, auc=_auc(clipped, actual),
        base_rate=_mean(actual), count=len(actual))


def _auc(probabilities: Sequence[float], actual: Sequence[float]) -> float:
    positives = [p for p, a in zip(probabilities, actual, strict=True)
                 if a >= 0.5]
    negatives = [p for p, a in zip(probabilities, actual, strict=True)
                 if a < 0.5]
    if not positives or not negatives:
        return 0.5
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed: float

    def to_dict(self) -> dict[str, Any]:
        return {"lower": self.lower, "upper": self.upper, "count": self.count,
                "mean_predicted": self.mean_predicted,
                "observed": self.observed}


@dataclass(frozen=True)
class CalibrationReport:
    bins: tuple[CalibrationBin, ...]
    expected_calibration_error: float
    brier: float
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "bins": [b.to_dict() for b in self.bins],
            "expected_calibration_error": self.expected_calibration_error,
            "brier": self.brier,
            "count": self.count,
        }


def calibration_report(probabilities: Sequence[float],
                       actual: Sequence[float],
                       *, bins: int = 5) -> CalibrationReport:
    if not actual:
        return CalibrationReport((), 0.0, 0.0, 0)
    edges = [i / bins for i in range(bins + 1)]
    output: list[CalibrationBin] = []
    weighted_error = 0.0
    for index in range(bins):
        lower, upper = edges[index], edges[index + 1]
        members = [
            (p, a) for p, a in zip(probabilities, actual, strict=True)
            if (lower <= p < upper) or (index == bins - 1 and p == upper)]
        if not members:
            output.append(CalibrationBin(lower, upper, 0, 0.0, 0.0))
            continue
        mean_predicted = _mean([p for p, _ in members])
        observed = _mean([a for _, a in members])
        weighted_error += (len(members) / len(actual)) * abs(
            mean_predicted - observed)
        output.append(CalibrationBin(lower, upper, len(members),
                                     mean_predicted, observed))
    brier = sum((p - a) ** 2 for p, a in zip(probabilities, actual,
                                             strict=True)) / len(actual)
    return CalibrationReport(tuple(output), weighted_error, brier, len(actual))


def platt_calibrate(raw: Sequence[float],
                    actual: Sequence[float]) -> tuple[float, float]:
    """Fit a Platt (1-D logistic) calibration on validation scores."""
    if not raw:
        return (1.0, 0.0)
    logits = [math.log(min(1 - 1e-9, max(1e-9, p)) /
                       (1 - min(1 - 1e-9, max(1e-9, p)))) for p in raw]
    slope, intercept = 1.0, 0.0
    rate = 0.1
    count = float(len(logits))
    for _ in range(300):
        grad_slope = 0.0
        grad_intercept = 0.0
        for logit, y in zip(logits, actual, strict=True):
            predicted = _sigmoid(slope * logit + intercept)
            error = predicted - y
            grad_slope += error * logit
            grad_intercept += error
        slope -= rate * grad_slope / count
        intercept -= rate * grad_intercept / count
    return (slope, intercept)


def apply_platt(probabilities: Sequence[float],
                parameters: tuple[float, float]) -> list[float]:
    slope, intercept = parameters
    out: list[float] = []
    for p in probabilities:
        clipped = min(1 - 1e-9, max(1e-9, p))
        logit = math.log(clipped / (1 - clipped))
        out.append(_sigmoid(slope * logit + intercept))
    return out


# --- training protocol --------------------------------------------------------

#: Concrete estimator unions (mypy-friendly, no runtime protocol checks).
_RegressorUnion = RidgeRegression | GradientBoostingRegressor | RandomForest
_ClassifierUnion = LogisticRegression | RandomForest
_AnyEstimator = _RegressorUnion | _ClassifierUnion


@dataclass(frozen=True)
class AlgorithmReport:
    algorithm: str
    trained: bool
    selection_metric: float | None
    metrics: Mapping[str, Any]
    hyperparameters: Mapping[str, Any]
    feature_importance: Mapping[str, float]
    model: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "trained": self.trained,
            "selection_metric": self.selection_metric,
            "metrics": dict(self.metrics),
            "hyperparameters": dict(self.hyperparameters),
            "feature_importance": dict(sorted(
                self.feature_importance.items())),
            "model": dict(self.model),
        }


@dataclass(frozen=True)
class TargetEvaluation:
    target: str
    feature_set: str
    status: str
    reason: str | None
    schema: Mapping[str, Any]
    feature_names: tuple[str, ...]
    split_sizes: Mapping[str, int]
    positive_rate: float | None
    baseline: Mapping[str, Any]
    selected_algorithm: str | None
    algorithms: tuple[AlgorithmReport, ...]
    calibration: Mapping[str, Any] | None
    uncertainty: Mapping[str, Any]
    model_id: str
    dataset_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "feature_set": self.feature_set,
            "status": self.status,
            "reason": self.reason,
            "schema": dict(self.schema),
            "feature_names": list(self.feature_names),
            "split_sizes": dict(self.split_sizes),
            "positive_rate": self.positive_rate,
            "baseline": dict(self.baseline),
            "selected_algorithm": self.selected_algorithm,
            "algorithms": [a.to_dict() for a in self.algorithms],
            "calibration": (dict(self.calibration)
                            if self.calibration is not None else None),
            "uncertainty": dict(self.uncertainty),
            "model_id": self.model_id,
            "dataset_id": self.dataset_id,
        }


def _is_classification(target: str) -> bool:
    return target in ("success", "blocked")


def evaluate_target(
    learning: dataset.LearningDataset,
    target: str,
    *,
    feature_set: str = features.FEATURE_PLANNING,
    min_train_rows: int = MIN_TRAIN_ROWS,
    min_validation_rows: int = MIN_VALIDATION_ROWS,
    min_test_rows: int = MIN_TEST_ROWS,
) -> TargetEvaluation:
    """Train, select and evaluate one target (fail-safe on tiny samples)."""
    if target not in dataset.TARGET_FIELDS:
        model.fail(model.E_MALFORMED, f"unknown target {target!r}")
    classification = _is_classification(target)
    schema = features.build_feature_schema(target, feature_set=feature_set)

    # Attach labels and split membership.
    labelled: list[tuple[dataset.Observation, float, str]] = []
    for observation in learning.observations:
        value = dataset.target_value(observation, target)
        if value is None:
            continue
        split = learning.splits.get(observation.observation_id,
                                    dataset.SPLIT_UNASSIGNED)
        labelled.append((observation, value, split))

    train = [item for item in labelled if item[2] == dataset.SPLIT_TRAIN]
    validation = [item for item in labelled
                  if item[2] == dataset.SPLIT_VALIDATION]
    test = [item for item in labelled if item[2] == dataset.SPLIT_TEST]
    split_sizes = {"train": len(train), "validation": len(validation),
                   "test": len(test)}
    positives = sum(1 for _, value, _ in labelled if value >= 0.5)
    positive_rate = positives / len(labelled) if labelled else None

    reason = _insufficiency_reason(
        classification, labelled, train, validation, test,
        min_train_rows=min_train_rows,
        min_validation_rows=min_validation_rows,
        min_test_rows=min_test_rows)
    encoder = features.fit_encoder(
        [item[0] for item in train], schema)
    if reason is not None:
        return TargetEvaluation(
            target=target, feature_set=feature_set,
            status=ST_INSUFFICIENT, reason=reason,
            schema=schema.to_dict(), feature_names=encoder.feature_names,
            split_sizes=split_sizes, positive_rate=positive_rate,
            baseline={}, selected_algorithm=None, algorithms=(),
            calibration=None,
            uncertainty={"status": "UNAVAILABLE",
                         "reason": "insufficient data"},
            model_id="", dataset_id=learning.dataset_id)

    encoder_warnings = list(encoder.warnings)
    train_x = encoder.transform([item[0] for item in train])
    train_y = [item[1] for item in train]
    validation_x = encoder.transform([item[0] for item in validation])
    validation_y = [item[1] for item in validation]
    test_x = encoder.transform([item[0] for item in test])
    test_y = [item[1] for item in test]

    reports: list[AlgorithmReport] = []
    fitted: list[tuple[str, _AnyEstimator]] = []
    baseline: dict[str, Any]
    if classification:
        baseline = _majority_baseline(train_y, test_y)
        for algorithm in CLASSIFICATION_ALGORITHMS:
            classifier = _classification_estimator(algorithm)
            classifier.fit(train_x, train_y)
            selection = classification_metrics(
                classifier.predict_proba(validation_x), validation_y)
            class_metrics = classification_metrics(
                classifier.predict_proba(test_x), test_y)
            reports.append(AlgorithmReport(
                algorithm=algorithm, trained=True,
                selection_metric=selection.log_loss,
                metrics=class_metrics.to_dict(),
                hyperparameters=_hyperparameters(classifier),
                feature_importance=_importance_map(
                    classifier.importances(encoder.dimension()),
                    encoder.feature_names),
                model=classifier.to_dict()))
            fitted.append((algorithm, classifier))
    else:
        baseline = _mean_baseline(train_y, test_y)
        for algorithm in REGRESSION_ALGORITHMS:
            regressor = _regression_estimator(algorithm)
            regressor.fit(train_x, train_y)
            regression_selection = regression_metrics(
                regressor.predict(validation_x), validation_y)
            reg_metrics = regression_metrics(
                regressor.predict(test_x), test_y)
            reports.append(AlgorithmReport(
                algorithm=algorithm, trained=True,
                selection_metric=regression_selection.mae,
                metrics=reg_metrics.to_dict(),
                hyperparameters=_hyperparameters(regressor),
                feature_importance=_importance_map(
                    regressor.importances(encoder.dimension()),
                    encoder.feature_names),
                model=regressor.to_dict()))
            fitted.append((algorithm, regressor))

    selected_name = _select(
        reports,
        order=(CLASSIFICATION_ALGORITHMS if classification
               else REGRESSION_ALGORITHMS))
    selected = next(est for name, est in fitted if name == selected_name)
    calibration: dict[str, Any] | None = None
    uncertainty: dict[str, Any]
    if classification:
        classifier = cast(_ClassifierUnion, selected)
        validation_raw = classifier.predict_proba(validation_x)
        parameters = platt_calibrate(validation_raw, validation_y)
        test_raw = classifier.predict_proba(test_x)
        before = calibration_report(test_raw, test_y)
        after = calibration_report(
            apply_platt(test_raw, parameters), test_y)
        calibration = {
            "method": "platt",
            "parameters": {"slope": parameters[0],
                           "intercept": parameters[1]},
            "before": before.to_dict(),
            "after": after.to_dict(),
            "improved_ece": (after.expected_calibration_error
                             <= before.expected_calibration_error),
        }
        uncertainty = {
            "kind": "probability",
            "test_brier": after.brier,
            "expected_calibration_error": after.expected_calibration_error,
            "reason": "probabilities are calibrated point estimates, not "
                      "frequentist confidence intervals",
        }
    else:
        regressor = cast(_RegressorUnion, selected)
        validation_predictions = regressor.predict(validation_x)
        validation_metrics = regression_metrics(
            validation_predictions, validation_y)
        uncertainty = {
            "kind": "residual_interval",
            "residual_std": validation_metrics.residual_std,
            "interval_95": [
                round(-1.96 * validation_metrics.residual_std, 4),
                round(1.96 * validation_metrics.residual_std, 4)],
            "reason": "95% interval from validation residuals under a "
                      "zero-mean, constant-variance assumption",
        }

    selected_metrics = next(
        r.metrics for r in reports if r.algorithm == selected_name)
    evaluation = TargetEvaluation(
        target=target, feature_set=feature_set, status=ST_TRAINED,
        reason=None, schema={**schema.to_dict(),
                             "schema_id": schema.schema_id},
        feature_names=encoder.feature_names, split_sizes=split_sizes,
        positive_rate=positive_rate, baseline=baseline,
        selected_algorithm=selected_name, algorithms=tuple(reports),
        calibration=calibration, uncertainty=uncertainty,
        model_id="", dataset_id=learning.dataset_id)
    metadata = {
        "encoder": encoder.to_dict(),
        "selected": selected.to_dict(),
        "selected_metrics": selected_metrics,
        "baseline": baseline,
        "warnings": encoder_warnings,
    }
    model_id = model.digest(
        {"target": target, "feature_set": feature_set,
         "dataset_id": learning.dataset_id, "metadata": metadata},
        domain=model.MATERIAL_DOMAIN)
    return replace(evaluation, model_id=model_id,
                   uncertainty={**uncertainty, "metadata": metadata})


def _insufficiency_reason(
    classification: bool,
    labelled: Sequence[tuple[dataset.Observation, float, str]],
    train: Sequence[tuple[dataset.Observation, float, str]],
    validation: Sequence[tuple[dataset.Observation, float, str]],
    test: Sequence[tuple[dataset.Observation, float, str]],
    *, min_train_rows: int, min_validation_rows: int, min_test_rows: int,
) -> str | None:
    if len(labelled) == 0:
        return "no labelled rows for this target"
    if classification:
        positives = sum(1 for _, value, _ in labelled if value >= 0.5)
        negatives = len(labelled) - positives
        if positives < MIN_PER_CLASS or negatives < MIN_PER_CLASS:
            return (f"only {positives} positive / {negatives} negative "
                    f"rows; {MIN_PER_CLASS} of each required")
    if len(train) < min_train_rows:
        return f"{len(train)} training rows < {min_train_rows}"
    if len(validation) < min_validation_rows:
        return f"{len(validation)} validation rows < {min_validation_rows}"
    if len(test) < min_test_rows:
        return f"{len(test)} test rows < {min_test_rows}"
    return None


def _regression_estimator(algorithm: str) -> _RegressorUnion:
    if algorithm == ALG_RIDGE:
        return RidgeRegression()
    if algorithm == ALG_FOREST:
        return RandomForest(classification=False)
    if algorithm == ALG_BOOSTING:
        return GradientBoostingRegressor()
    model.fail(model.E_MALFORMED, f"unknown algorithm {algorithm!r}")


def _classification_estimator(algorithm: str) -> _ClassifierUnion:
    if algorithm == ALG_LOGISTIC:
        return LogisticRegression()
    if algorithm == ALG_FOREST:
        return RandomForest(classification=True)
    model.fail(model.E_MALFORMED, f"unknown algorithm {algorithm!r}")


def _hyperparameters(estimator: _AnyEstimator) -> dict[str, Any]:
    document = estimator.to_dict()
    hyperparameters = document.get("hyperparameters", {})
    return dict(hyperparameters) if isinstance(hyperparameters, Mapping) else {}


def _importance_map(values: Sequence[float],
                    names: Sequence[str]) -> dict[str, float]:
    paired = sorted(
        zip(names, values, strict=True), key=lambda item: (-item[1], item[0]))
    return {name: round(value, 6) for name, value in paired if value > 0.0}


def _select(reports: Sequence[AlgorithmReport], *,
            order: Sequence[str] = REGRESSION_ALGORITHMS) -> str:
    trained = [r for r in reports if r.trained and r.selection_metric is not None]
    if not trained:
        model.fail(model.E_INSUFFICIENT_DATA, "no trained algorithm")
    ordering = {name: index for index, name in enumerate(order)}
    best = min(
        trained,
        key=lambda r: (r.selection_metric or math.inf,
                       ordering.get(r.algorithm, 99)))
    return best.algorithm


def _mean_baseline(train_y: Sequence[float],
                   test_y: Sequence[float]) -> dict[str, Any]:
    mean = _mean(train_y)
    predictions = [mean] * len(test_y)
    metrics = regression_metrics(predictions, test_y)
    return {"algorithm": ALG_MEAN, "value": mean, "metrics": metrics.to_dict()}


def _majority_baseline(train_y: Sequence[float],
                       test_y: Sequence[float]) -> dict[str, Any]:
    positives = sum(1 for value in train_y if value >= 0.5)
    probability = positives / len(train_y) if train_y else 0.0
    predictions = [probability] * len(test_y)
    metrics = classification_metrics(predictions, test_y)
    return {"algorithm": ALG_MAJORITY, "positive_rate": probability,
            "metrics": metrics.to_dict()}


# --- report -------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingReport:
    dataset_id: str
    feature_set: str
    generated_at: str
    evaluations: tuple[TargetEvaluation, ...]

    def evaluation(self, target: str) -> TargetEvaluation | None:
        for item in self.evaluations:
            if item.target == target:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "intelligence_version": model.INTELLIGENCE_VERSION,
            "kind": "ml_training_report",
            "dataset_id": self.dataset_id,
            "feature_set": self.feature_set,
            "generated_at": self.generated_at,
            "evaluations": [e.to_dict() for e in self.evaluations],
        }

    def render_markdown(self) -> str:
        lines = [
            "# M057 — Predictive ML evaluation report", "",
            f"- dataset: `{self.dataset_id}`",
            f"- feature set: `{self.feature_set}`",
            f"- generated: {self.generated_at}", "",
            "| target | status | selected | test metric | baseline |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in self.evaluations:
            if item.status != ST_TRAINED:
                lines.append(
                    f"| {item.target} | INSUFFICIENT_DATA | - | - | "
                    f"{item.reason} |")
                continue
            selected = next(
                a for a in item.algorithms
                if a.algorithm == item.selected_algorithm)
            metric = _primary_metric(item.target, selected.metrics)
            baseline_metric = _primary_metric(item.target,
                                              item.baseline["metrics"])
            lines.append(
                f"| {item.target} | TRAINED | {item.selected_algorithm} | "
                f"{metric} | {baseline_metric} |")
        return "\n".join(lines) + "\n"


def _primary_metric(target: str, metrics: Mapping[str, Any]) -> str:
    if _is_classification(target):
        return (f"log_loss={metrics.get('log_loss'):.4f} "
                f"auc={metrics.get('auc'):.4f} "
                f"acc={metrics.get('accuracy'):.4f}")
    return (f"mae={metrics.get('mae'):.4f} "
            f"rmse={metrics.get('rmse'):.4f} "
            f"r2={metrics.get('r2'):.4f}")


def train_all(
    learning: dataset.LearningDataset, *,
    feature_set: str = features.FEATURE_PLANNING,
    targets: Sequence[str] = dataset.TARGET_FIELDS,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> TrainingReport:
    """Train and evaluate every target where the data permits."""
    evaluations = tuple(
        evaluate_target(learning, target, feature_set=feature_set)
        for target in targets)
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    return TrainingReport(
        dataset_id=learning.dataset_id, feature_set=feature_set,
        generated_at=stamp, evaluations=evaluations)


def models_path(root: str) -> str:
    return f"{root}/models/training-report.json"


def persist_report(root: str, report: TrainingReport) -> str:
    model.write_json(models_path(root), report.to_dict())
    return models_path(root)


def load_report(root: str) -> TrainingReport | None:
    document = model.read_json(models_path(root))
    if document is None:
        return None
    return report_from_dict(document)


def report_from_dict(document: Mapping[str, Any]) -> TrainingReport:
    model.check_version(document, "training report")
    evaluations = tuple(
        _evaluation_from_dict(model.require_mapping(item, "evaluation"))
        for item in model.require_list(
            document.get("evaluations", []), "evaluations"))
    return TrainingReport(
        dataset_id=str(document.get("dataset_id", "")),
        feature_set=str(document.get("feature_set", "")),
        generated_at=str(document.get("generated_at", "")),
        evaluations=evaluations)


def _evaluation_from_dict(mapping: Mapping[str, Any]) -> TargetEvaluation:
    algorithms = tuple(
        AlgorithmReport(
            algorithm=str(item.get("algorithm", "")),
            trained=bool(item.get("trained", False)),
            selection_metric=(
                float(item["selection_metric"])
                if item.get("selection_metric") is not None else None),
            metrics=dict(model.require_mapping(item.get("metrics", {}),
                                               "metrics")),
            hyperparameters=dict(model.require_mapping(
                item.get("hyperparameters", {}), "hyperparameters")),
            feature_importance={
                str(k): float(v) for k, v in model.require_mapping(
                    item.get("feature_importance", {}),
                    "feature_importance").items()},
            model=dict(model.require_mapping(item.get("model", {}),
                                             "model")),
        )
        for item in model.require_list(
            mapping.get("algorithms", []), "algorithms"))
    calibration = mapping.get("calibration")
    return TargetEvaluation(
        target=str(mapping.get("target", "")),
        feature_set=str(mapping.get("feature_set", "")),
        status=str(mapping.get("status", "")),
        reason=(str(mapping["reason"])
                if mapping.get("reason") is not None else None),
        schema=dict(model.require_mapping(mapping.get("schema", {}),
                                          "schema")),
        feature_names=tuple(str(v) for v in model.require_list(
            mapping.get("feature_names", []), "feature_names")),
        split_sizes={
            str(k): int(v) for k, v in model.require_mapping(
                mapping.get("split_sizes", {}), "split_sizes").items()},
        positive_rate=(
            float(mapping["positive_rate"])
            if mapping.get("positive_rate") is not None else None),
        baseline=dict(model.require_mapping(mapping.get("baseline", {}),
                                            "baseline")),
        selected_algorithm=(
            str(mapping["selected_algorithm"])
            if mapping.get("selected_algorithm") is not None else None),
        algorithms=algorithms,
        calibration=(dict(calibration)
                     if isinstance(calibration, Mapping) else None),
        uncertainty=dict(model.require_mapping(
            mapping.get("uncertainty", {}), "uncertainty")),
        model_id=str(mapping.get("model_id", "")),
        dataset_id=str(mapping.get("dataset_id", "")),
    )


__all__ = [
    "ALG_BOOSTING",
    "ALG_DECISION_TREE",
    "ALG_FOREST",
    "ALG_LOGISTIC",
    "ALG_MAJORITY",
    "ALG_MEAN",
    "ALG_RIDGE",
    "AlgorithmReport",
    "CalibrationReport",
    "ClassificationMetrics",
    "DecisionTreeClassifier",
    "DecisionTreeRegressor",
    "GradientBoostingRegressor",
    "LogisticRegression",
    "RandomForest",
    "RegressionMetrics",
    "RidgeRegression",
    "ST_INSUFFICIENT",
    "ST_TRAINED",
    "TargetEvaluation",
    "TrainingReport",
    "TreeNode",
    "apply_platt",
    "calibration_report",
    "classification_metrics",
    "evaluate_target",
    "load_report",
    "models_path",
    "persist_report",
    "platt_calibrate",
    "regression_metrics",
    "report_from_dict",
    "train_all",
]
