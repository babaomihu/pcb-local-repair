"""Explicit additive routing permissions, shared by generator and acceptance gate."""
import re


def permissions(task, copper_layers=None):
    default = {"schema_version": 1, "route_layers": [task["allowed_layer"]],
               "max_new_vias": 0, "via_type": "through"}
    value = task.get("permissions", default)
    if not isinstance(value, dict) or set(value) != set(default):
        raise ValueError("Unknown or incomplete task permission contract")
    layers = value["route_layers"]
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["via_type"] != "through"
            or not isinstance(layers, list) or not layers
            or any(not isinstance(x, str) or not re.fullmatch(r"(?:F|B|In[1-9][0-9]*)\.Cu", x) for x in layers)
            or len(layers) != len(set(layers)) or task["allowed_layer"] not in layers
            or type(value["max_new_vias"]) is not int or not 0 <= value["max_new_vias"] <= 32):
        raise ValueError("Invalid route layers or ordinary through-via budget")
    if copper_layers is not None:
        if not set(layers) <= set(copper_layers):
            raise ValueError("Authorized routing layer does not exist on this board")
        if value["max_new_vias"] and set(layers) != set(copper_layers):
            raise ValueError("Through vias require explicit authorization for every copper layer they cross")
    if value["max_new_vias"] and not {"F.Cu", "B.Cu"} <= set(layers):
        raise ValueError("Through vias require both outer copper layers")
    return dict(value, route_layers=list(layers))
