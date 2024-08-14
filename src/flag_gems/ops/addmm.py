import logging

import torch
import triton
import triton.language as tl

from ..utils import libentry


@libentry()
@triton.autotune(
    configs=[
        triton.Config(
            {'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 128},
            num_warps=1,
            num_stages=4,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 256, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 128},
            num_warps=1,
            num_stages=4,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 256, 'BLOCK_SIZE_N': 256, 'BLOCK_SIZE_K': 128},
            num_warps=1,
            num_stages=4,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 256, 'BLOCK_SIZE_N': 1024, 'BLOCK_SIZE_K': 64},
            num_warps=4,
            num_stages=1,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 64, 'BLOCK_SIZE_N': 512, 'BLOCK_SIZE_K': 128},
            num_warps=4,
            num_stages=4,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 512, 'BLOCK_SIZE_K': 128},
            num_warps=4,
            num_stages=4,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 256, 'BLOCK_SIZE_N': 512, 'BLOCK_SIZE_K': 128},
            num_warps=4,
            num_stages=4,
        ),
        triton.Config(
            {'BLOCK_SIZE_M': 256, 'BLOCK_SIZE_N': 1024, 'BLOCK_SIZE_K': 128},
            num_warps=4,
            num_stages=4,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 64, "BLOCK_SIZE_N": 64, "BLOCK_SIZE_K": 64},
            num_warps=1,
            num_stages=4,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 64, "BLOCK_SIZE_N": 64, "BLOCK_SIZE_K": 384},
            num_warps=1,
            num_stages=1,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 768, "BLOCK_SIZE_N": 64, "BLOCK_SIZE_K": 256},
            num_warps=4,
            num_stages=4,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 192, "BLOCK_SIZE_N": 128, "BLOCK_SIZE_K": 256},
            num_warps=1,
            num_stages=4,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 256, "BLOCK_SIZE_N": 768, "BLOCK_SIZE_K": 256},
            num_warps=4,
            num_stages=4,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 256, "BLOCK_SIZE_N": 128, "BLOCK_SIZE_K": 128},
            num_warps=1,
            num_stages=4,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 256, "BLOCK_SIZE_N": 512, "BLOCK_SIZE_K": 256},
            num_warps=4,
            num_stages=5,
        ),
        triton.Config(
            {"BLOCK_SIZE_M": 128, "BLOCK_SIZE_N": 512, "BLOCK_SIZE_K": 256},
            num_warps=4,
            num_stages=5,
        ),
    ],
    key=["M", "N", "K"],
)
@triton.heuristics(
    {
        "EVEN_M": lambda args: args['M'] % args['BLOCK_SIZE_M'] == 0,
        "EVEN_N": lambda args: args['N'] % args['BLOCK_SIZE_N'] == 0,
        "EVEN_K": lambda args: args['K'] % args['BLOCK_SIZE_K'] == 0,
    }
)
@triton.jit(do_not_specialize=["alpha", "beta"])
def addmm_kernel(
    a_ptr,
    b_ptr,
    bias_ptr,
    c_ptr,
    alpha,
    beta,
    M,
    N,
    K,
    stride_am,
    stride_ak,
    stride_bk,
    stride_bn,
    stride_cm,
    stride_cn,
    BLOCK_SIZE_M: tl.constexpr,
    BLOCK_SIZE_N: tl.constexpr,
    BLOCK_SIZE_K: tl.constexpr,
    EVEN_M: tl.constexpr,
    EVEN_N: tl.constexpr,
    EVEN_K: tl.constexpr,
):
    pid_m = tl.program_id(0)
    pid_n = tl.program_id(1)

    offs_am = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
    offs_bn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
    offs_k = tl.arange(0, BLOCK_SIZE_K)
    a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
    b_ptrs = b_ptr + (offs_k[:, None] * stride_bk + offs_bn[None, :] * stride_bn)
    bias_ptrs = bias_ptr + offs_bn
    if EVEN_N:
        bias = tl.load(bias_ptrs)
    else:
        bias = tl.load(bias_ptrs, mask=offs_bn < N, other=0.0)
    accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_SIZE_K)):
        k_remaining = K - k * BLOCK_SIZE_K
        if EVEN_M and EVEN_K:
            a = tl.load(a_ptrs)
        else:        
            a = tl.load(
                a_ptrs,
                mask=(offs_am[:, None] < M) & (offs_k[None, :] < k_remaining),
                other=0.0,
            )
        if EVEN_N and EVEN_K:
            b = tl.load(b_ptrs)
        else:
            b = tl.load(
                b_ptrs,
                mask=(offs_k[:, None] < k_remaining) & (offs_bn[None, :] < N),
                other=0.0,
            )
        accumulator += tl.dot(a, b, allow_tf32=False)
        a_ptrs += BLOCK_SIZE_K * stride_ak
        b_ptrs += BLOCK_SIZE_K * stride_bk

    accumulator = accumulator * alpha + bias * beta
    c = accumulator.to(bias.dtype)

    offs_cm = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
    offs_cn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
    c_ptrs = c_ptr + stride_cm * offs_cm[:, None] + stride_cn * offs_cn[None, :]
    if EVEN_M and EVEN_N:
        tl.store(c_ptrs, c)
    else:
        c_mask = (offs_cm[:, None] < M) & (offs_cn[None, :] < N)
        tl.store(c_ptrs, c, mask=c_mask)


def addmm(bias, mat1, mat2, *, beta=1, alpha=1):
    logging.debug("GEMS ADDMM")
    assert mat1.shape[1] == mat2.shape[0], "Incompatible dimensions"
    M, K = mat1.shape
    _, N = mat2.shape

    mat1 = mat1.contiguous()
    mat2 = mat2.contiguous()
    out = torch.empty((M, N), device=mat1.device, dtype=mat1.dtype)

    grid = lambda META: (
        triton.cdiv(M, META["BLOCK_SIZE_M"]),
        triton.cdiv(N, META["BLOCK_SIZE_N"]),
    )
    with torch.cuda.device(mat1.device):
        addmm_kernel[grid](
            mat1,
            mat2,
            bias,
            out,
            alpha,
            beta,
            M,
            N,
            K,
            mat1.stride(0),
            mat1.stride(1),
            mat2.stride(0),
            mat2.stride(1),
            out.stride(0),
            out.stride(1),
        )
    return out
