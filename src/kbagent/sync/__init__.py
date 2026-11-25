"""同步通道预留。不做移动端，也不做传输。"""

from .ids import mint_stable_id, parse_stable_id
from .permissions import CHANNEL_KINDS, GRANT_NAMES, allows
from .service import COPIES_ORIGINAL, MOBILE_CLIENT, TRANSPORT_IMPLEMENTED, SyncReservation

__all__ = [
    "CHANNEL_KINDS",
    "COPIES_ORIGINAL",
    "GRANT_NAMES",
    "MOBILE_CLIENT",
    "SyncReservation",
    "TRANSPORT_IMPLEMENTED",
    "allows",
    "mint_stable_id",
    "parse_stable_id",
]
