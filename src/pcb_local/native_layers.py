"""Use native physical copper IDs for machine decisions, retaining display aliases."""
import re


def copper_layer_identity(board):
    rows = [
        {"native_id": int(layer),
         "canonical_name": str(board.GetStandardLayerName(layer)),
         "display_name": str(board.GetLayerName(layer))}
        for layer in board.GetEnabledLayers().CuStack()
    ]
    names = [row["canonical_name"] for row in rows]
    ids = [row["native_id"] for row in rows]
    if (not rows or len(set(ids)) != len(ids) or len(set(names)) != len(names)
            or any(not re.fullmatch(r"(F|B|In[1-9][0-9]*)\.Cu", name) for name in names)):
        raise ValueError("Native copper layer identity is incomplete or ambiguous")
    # Aliases may repeat or resemble another physical layer. They are labels,
    # never lookup keys for routing, clearance projection or fill protection.
    return rows


def copper_layer_id(board, canonical_name):
    matches = [row["native_id"] for row in copper_layer_identity(board)
               if row["canonical_name"] == canonical_name]
    if len(matches) != 1:
        raise ValueError("Requested standard copper layer is not enabled")
    return matches[0]
