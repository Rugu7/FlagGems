import logging
import math

import torch
import triton
import triton.language as tl

from ..utils import libentry, cfggen_reduce_op2, TOTAL_CORE_NUM, count_divisible_by_2


@triton.jit
def reduce_mul(a, b):
    return a * b


@libentry()
@triton.autotune(configs=cfggen_reduce_op2(), key=["M"])
@triton.jit
def prod_kernel_mid(
    inp,
    mid,
    M,
    BLOCK_SIZE: tl.constexpr,
    ITER_NUM: tl.constexpr,
):
    pid = tl.program_id(0)
    num_jobs = tl.num_programs(axis=0)
    block_start = pid * BLOCK_SIZE
    step = num_jobs * BLOCK_SIZE
    _tmp = tl.full([BLOCK_SIZE], value=1.0, dtype=tl.float32)
    block_start = block_start.to(tl.int64)
    for off in range(block_start, M, step):
        offset = off + tl.arange(0, BLOCK_SIZE)
        mask = offset < M
        inp_val = tl.load(inp + offset, mask=mask, other=1.0).to(tl.float32)
        _tmp = inp_val * _tmp

    # Reset to original reduce programming mode after optimizing the tl.reduce.
    for x in tl.static_range(1, int(ITER_NUM), 1):
        _tmp[:BLOCK_SIZE // (2 ** x)] = _tmp[:BLOCK_SIZE // (2 ** x)] * _tmp[BLOCK_SIZE // (2 ** x):(BLOCK_SIZE // (2 ** x)) * 2]

    mid_ptr = mid + pid
    tl.store(mid_ptr, _tmp[0])


@libentry()
@triton.jit
def prod_kernel_result(mid, out, mid_size: tl.constexpr, loop_num: tl.constexpr):
    offset = tl.arange(0, mid_size)
    mid_val = tl.load(mid + offset)

    # Reset to original reduce programming mode after optimizing the tl.reduce.
    for x in tl.static_range(1, loop_num, 1):
        mid_val[:mid_size // (2 ** x)] = mid_val[:mid_size // (2 ** x)] * mid_val[mid_size // (2 ** x):(mid_size // (2 ** x)) * 2]

    prod_val = tl.reduce(mid_val[:mid_size // (2 ** (loop_num - 1))], axis=0, combine_fn=reduce_mul)
    tl.store(out, prod_val)


def prod(inp, *, dtype=None):
    logging.debug("GEMS PROD")
    if dtype is None:
        dtype = inp.dtype

    M = inp.numel()
    grid = lambda meta: (min(triton.cdiv(M, meta['BLOCK_SIZE']), TOTAL_CORE_NUM), )
    mid_size = TOTAL_CORE_NUM
    loop_num = count_divisible_by_2(mid_size) + 1

    mid = torch.ones((mid_size,), dtype=dtype, device=inp.device)
    out = torch.empty([], dtype=dtype, device=inp.device)

    with torch.mlu.device(inp.device):
        prod_kernel_mid[grid](inp, mid, M)
        prod_kernel_result[(1, 1, 1)](mid, out, mid_size, loop_num)
    return out


def heur_block_n(args):
    return triton.next_power_of_2(args["N"])


@libentry()
@triton.autotune(
    configs=[
        triton.Config({"BLOCK_M": 8}, num_warps=8, num_stages=4),
        triton.Config({"BLOCK_M": 8}, num_warps=8, num_stages=5),
        triton.Config({"BLOCK_M": 16}, num_warps=8, num_stages=4),
        triton.Config({"BLOCK_M": 16}, num_warps=8, num_stages=5),
        triton.Config({"BLOCK_M": 32}, num_warps=8, num_stages=4),
        triton.Config({"BLOCK_M": 32}, num_warps=8, num_stages=5),
    ],
    key=[
        "M",
        "N",
    ],
)
@triton.heuristics(
    {
        "BLOCK_N": heur_block_n,
    }
)
@triton.jit
def prod_kernel(
    inp,
    out,
    M,
    N,
    K,
    BLOCK_M: tl.constexpr,
    BLOCK_N: tl.constexpr,
):
    # set offset
    pid_m = tl.program_id(0)
    pid_k = tl.program_id(1)
    m_offset = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    n_offset = tl.arange(0, BLOCK_N)
    offset = m_offset[:, None] * N * K + n_offset[None, :] * K + pid_k
    offset_index = m_offset * K + pid_k
    # set mask
    mask1 = m_offset < M
    mask = m_offset[:, None] < M and n_offset[None, :] < N
    inp_ptrs = inp + offset
    inp_vals = tl.load(inp_ptrs, mask=mask, other=1.0).to(tl.float32)
    result_index = tl.reduce(inp_vals, axis=1, combine_fn=reduce_mul)

    out_ptrs = out + offset_index
    tl.store(out_ptrs, result_index, mask=mask1)


def prod_dim(inp, dim=None, keepdim=False, *, dtype=None):
    logging.debug("GEMS PROD DIM")

    assert dim >= -inp.ndim and dim < inp.ndim, "Invalid dim"
    shape = inp.shape
    dim = dim % inp.ndim
    N = shape[dim]
    M = math.prod(shape[:dim])
    K = inp.numel() // M // N

    inp = inp.contiguous()

    shape_list = list(shape)
    shape_list[dim] = 1

    if dtype is None:
        dtype = inp.dtype
    out = torch.empty(shape_list, dtype=dtype, device=inp.device)
    if not keepdim:
        out = torch.squeeze(out, dim)

    grid = lambda meta: (
        triton.cdiv(M, meta["BLOCK_M"]),
        K,
    )
    with torch.mlu.device(inp.device):
        prod_kernel[grid](inp, out, M, N, K)

    return out
