from uuid import UUID, uuid5

IMPORT_NAMESPACE_V1 = UUID("fa40cce6-177a-587f-8646-287d21817762")


def canonicalize_import_id(kind: str, source_namespace: str, external_id: str) -> UUID:
    """
    Generate a canonical UUID for an imported entity based on its kind,
    source namespace, and external ID.

    Args:
        kind: The type of entity ('portfolio', 'entity', or 'relation')
        source_namespace: The source namespace identifier
        external_id: The external ID from the source

    Returns:
        A UUID v5 that uniquely identifies this import

    Raises:
        ValueError: If kind is not one of the allowed values
    """
    # Validate kind
    if kind not in {"portfolio", "entity", "relation"}:
        raise ValueError(f"Invalid kind '{kind}'. Must be 'portfolio', 'entity', or 'relation'.")

    # Generate UUIDs according to the identity contract
    kind_namespace = uuid5(IMPORT_NAMESPACE_V1, kind)
    source_namespace_uuid = uuid5(kind_namespace, source_namespace)
    canonical_id = uuid5(source_namespace_uuid, external_id)

    return canonical_id
