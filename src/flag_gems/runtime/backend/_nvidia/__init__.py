from backend_utils import Autograd, VendorInfoBase  # noqa: E402

from .ops import *  # noqa: F403

vendor_info = VendorInfoBase(
    vendor_name="nvidia", device_name="cuda", device_query_cmd="rocm-smi"
)


def get_register_op_config():
    return (("add.Tensor", add, Autograd.disable),)


def get_unused_op():
    return ("cumsum", "cos")


__all__ = ["*"]
