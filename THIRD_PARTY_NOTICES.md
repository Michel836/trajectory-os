# TrajectoryOS Third-Party Notices

TrajectoryOS uses third-party software distributed under licenses that are
separate from the TrajectoryOS license.

The presence of a dependency in this document does not mean that the dependency
is relicensed under the GNU Affero General Public License v3.0. Each third-party
component remains subject to its own copyright and license terms.

This inventory reflects the dependency graph locked by `uv.lock` for
TrajectoryOS v0.1.0 at the time this document was generated.

## Runtime dependencies

| Package | Version | Relationship | License |
| --- | --- | --- | --- |
| DuckDB | 1.5.5 | Direct | MIT |
| NetworkX | 3.6.1 | Direct | BSD-3-Clause |
| Pydantic | 2.13.4 | Direct | MIT |
| SQLAlchemy | 2.0.52 | Direct | MIT |
| annotated-types | 0.8.0 | Transitive via Pydantic | MIT |
| greenlet | 3.5.5 | Transitive via SQLAlchemy | MIT AND PSF-2.0 |
| pydantic-core | 2.46.4 | Transitive via Pydantic | MIT |
| typing-extensions | 4.16.0 | Transitive | PSF-2.0 |
| typing-inspection | 0.4.4 | Transitive via Pydantic | MIT |

## Development dependencies

| Package | Version | Relationship | License |
| --- | --- | --- | --- |
| mypy | 2.3.1 | Direct development dependency | MIT |
| pytest | 9.1.1 | Direct development dependency | MIT |
| pytest-cov | 7.1.0 | Direct development dependency | MIT |
| Ruff | 0.16.4 | Direct development dependency | MIT |
| ast-serialize | 0.8.0 | Transitive via mypy | MIT |
| coverage | 7.15.4 | Transitive via pytest-cov | Apache-2.0 |
| iniconfig | 2.3.0 | Transitive via pytest | MIT |
| librt | 0.15.0 | Transitive via mypy | MIT |
| mypy-extensions | 1.1.0 | Transitive via mypy | MIT |
| packaging | 26.3 | Transitive | Apache-2.0 OR BSD-2-Clause |
| pathspec | 1.1.1 | Transitive via mypy | MPL-2.0 |
| pluggy | 1.6.0 | Transitive via pytest / pytest-cov | MIT |
| Pygments | 2.21.0 | Transitive via pytest | BSD-2-Clause |

## Conditional locked dependency

| Package | Version | Relationship | License |
| --- | --- | --- | --- |
| colorama | 0.4.6 | Conditional platform dependency present in `uv.lock` | BSD-3-Clause |

`colorama` is present in the lock file but is not installed in the current
Linux development environment because its dependency condition is
platform-specific.

## License compatibility review

The current dependency inventory contains permissive licenses and one
file-level copyleft development dependency (`pathspec`, MPL-2.0).

No dependency incompatibility has been identified with TrajectoryOS being
distributed under `AGPL-3.0-only`.

In particular, `pathspec` is a development-time transitive dependency and is
not incorporated into TrajectoryOS source code. Its MPL-2.0 terms continue to
apply independently to `pathspec` itself.

## Distribution responsibility

This repository does not vendor the third-party packages listed above.

Anyone distributing TrajectoryOS together with third-party software is
responsible for complying with all applicable third-party license requirements,
including preservation of copyright notices, license texts, attribution, or
other notices where required.

The authoritative license terms are those distributed by each upstream
project, not this summary.

## Maintenance

This file should be reviewed whenever:

- a dependency is added or removed;
- a dependency changes license;
- dependency versions are materially updated;
- TrajectoryOS begins vendoring third-party source or binary components;
- packaging or distribution practices change.
