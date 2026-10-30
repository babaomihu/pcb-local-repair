"""Witnessed standard stacks and distinct serialized/native KiCad layer IDs.

Pinned engine PCB_LAYER_ID (include/layer_ids.h) uses even IDs. The KiCad 9
board-file syntax instead serializes inner copper as 1..30 and B.Cu as 31.
"""
import re

SUPPORTED_COUNTS = (2, 4, 6, 8)


def standard_stack(count):
    if count not in SUPPORTED_COUNTS:
        raise ValueError('PNS supports only witnessed standard 2/4/6/8-layer stacks')
    return ['F.Cu', *('In%d.Cu' % n for n in range(1, count - 1)), 'B.Cu']


def supported_stack(layers):
    return isinstance(layers, list) and len(layers) in SUPPORTED_COUNTS and layers == standard_stack(len(layers))


def serialized_copper_id(name):
    if name == 'F.Cu':
        return 0
    if name == 'B.Cu':
        return 31
    match = re.fullmatch(r'In([1-6])\.Cu', name)
    if not match:
        raise ValueError('Copper layer is outside the witnessed 2/4/6/8-layer scope: ' + str(name))
    return int(match.group(1))


def native_copper_id(name):
    if name == 'B.Cu':
        return 2
    index = serialized_copper_id(name)
    return 0 if index == 0 else 2 * (index + 1)
