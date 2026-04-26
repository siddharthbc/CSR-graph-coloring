#!/usr/bin/env python3
"""
Compile Picasso CSL code on the Cerebras CS-3 Cloud (appliance mode).

Uses SdkCompiler to launch a compile job on the Wafer-Scale Cluster.
Produces an artifact_path.json that the runner script reads.

Usage:
    python compile_appliance.py \
        --num-cols 2 --num-rows 1 \
        --max-local-verts 64 --max-local-edges 256 \
        --max-boundary 128 --max-relay 256 \
        --palette-size 16
"""

import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description='Compile Picasso CSL for Cerebras appliance')
    parser.add_argument('--num-cols', type=int, default=2)
    parser.add_argument('--num-rows', type=int, default=1)
    parser.add_argument('--max-local-verts', type=int, default=64)
    parser.add_argument('--max-local-edges', type=int, default=256)
    parser.add_argument('--max-boundary', type=int, default=128)
    parser.add_argument('--max-relay', type=int, default=256)
    parser.add_argument('--max-palette-size', type=int, default=64,
                        help='Compile-time max palette size (sizes forbidden[] array)')
    parser.add_argument('--max-list-size', type=int, default=16,
                        help='Max colors per vertex list (Picasso T parameter)')
    parser.add_argument('--routing-mode', type=int, default=0,
                        help='Routing mode: 0=SW relay (default), 1=HW filter')
    parser.add_argument('--csl-dir', type=str, default=None,
                        help='Path to CSL source directory (default: ../csl)')
    parser.add_argument('--output', type=str, default='artifact_path.json',
                        help='Output JSON file with artifact path')
    parser.add_argument('--hardware', action='store_true',
                        help='Compile for real hardware (full fabric dims)')
    parser.add_argument('--lww-layout', type=str, default=None,
                        choices=[None, 'bidir', 'east', 'east_seg', '2d',
                                 '2d_seg', '2d_seg2', '2d_multicast'],
                        help='Pipelined-LWW layout to compile (overrides default sw-relay layout.csl)')
    parser.add_argument('--seg-size', type=int, default=2,
                        help='Segment size S for segmented LWW kernels (default 2). '
                             'For 2d_seg2, this is S_row; col-axis uses --s-col.')
    parser.add_argument('--s-col', type=int, default=None,
                        help='Col-axis segment size for 2d_seg2 (default = same '
                             'as --seg-size). Set to 1 to drop south_slot_count to '
                             '1 globally and free Q3 IQ for the dedicated '
                             'back-channel (CP2d.e). Ignored by other layouts.')
    args = parser.parse_args()

    # Locate CSL sources
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    csl_dir = args.csl_dir or os.path.join(repo_root, 'csl')

    # Pick the right layout file based on --lww-layout
    layout_map = {
        None:           'layout.csl',
        'bidir':        'layout_lww.csl',
        'east':         'layout_lww_east.csl',
        'east_seg':     'layout_lww_east_seg.csl',
        '2d':           'layout_lww_2d.csl',
        '2d_seg':       'layout_lww_2d_seg.csl',
        '2d_seg2':      'layout_lww_2d_seg2.csl',
        '2d_multicast': 'layout_lww_2d_multicast.csl',
    }
    layout_file = layout_map[args.lww_layout]

    if not os.path.isfile(os.path.join(csl_dir, layout_file)):
        print(f"ERROR: {layout_file} not found in {csl_dir}")
        sys.exit(1)

    # Import Cerebras appliance SDK
    try:
        from cerebras.sdk.client import SdkCompiler
    except ImportError:
        print("ERROR: cerebras_sdk not installed.")
        print("Install with: pip install cerebras_sdk==2.5.0")
        sys.exit(1)

    # Fabric dimensions
    # For simulator: minimal dims (PEs + memcpy overhead)
    # For hardware: full WSE-3 dims (762x1172)
    if args.hardware:
        # WSE-3 full dimensions (from ALCF docs)
        fabric_w = 762
        fabric_h = 1172
    else:
        fabric_w = args.num_cols + 8
        fabric_h = args.num_rows + 2

    # Build compiler arguments (same as cslc CLI)
    if args.lww_layout is None:
        # sw-relay / hw-filter need max_relay + routing_mode
        params = (
            f"num_cols:{args.num_cols},"
            f"num_rows:{args.num_rows},"
            f"max_local_verts:{args.max_local_verts},"
            f"max_local_edges:{args.max_local_edges},"
            f"max_boundary:{args.max_boundary},"
            f"max_relay:{args.max_relay},"
            f"max_palette_size:{args.max_palette_size},"
            f"max_list_size:{args.max_list_size},"
            f"routing_mode:{args.routing_mode}"
        )
    elif args.lww_layout in ('east_seg', '2d_seg'):
        # segmented LWW: takes S (single axis)
        params = (
            f"num_cols:{args.num_cols},"
            f"num_rows:{args.num_rows},"
            f"max_local_verts:{args.max_local_verts},"
            f"max_local_edges:{args.max_local_edges},"
            f"max_boundary:{args.max_boundary},"
            f"max_palette_size:{args.max_palette_size},"
            f"max_list_size:{args.max_list_size},"
            f"S:{args.seg_size}"
        )
    elif args.lww_layout == '2d_seg2':
        # 2d_seg2: per-axis S split (CP2d.e). Defaults to S_col=S_row
        # for backward compat; override with --s-col 1 to free Q3 IQ
        # for the dedicated back-channel.
        s_col = args.s_col if args.s_col is not None else args.seg_size
        params = (
            f"num_cols:{args.num_cols},"
            f"num_rows:{args.num_rows},"
            f"max_local_verts:{args.max_local_verts},"
            f"max_local_edges:{args.max_local_edges},"
            f"max_boundary:{args.max_boundary},"
            f"max_palette_size:{args.max_palette_size},"
            f"max_list_size:{args.max_list_size},"
            f"S_row:{args.seg_size},"
            f"S_col:{s_col}"
        )
    else:
        # other lww layouts (bidir, east, 2d, 2d_multicast)
        params = (
            f"num_cols:{args.num_cols},"
            f"num_rows:{args.num_rows},"
            f"max_local_verts:{args.max_local_verts},"
            f"max_local_edges:{args.max_local_edges},"
            f"max_boundary:{args.max_boundary},"
            f"max_palette_size:{args.max_palette_size},"
            f"max_list_size:{args.max_list_size}"
        )

    # --arch flag: wse2 (default) for simulator, wse3 for CS-3 hardware
    arch_flag = "--arch=wse3 " if args.hardware else ""

    compiler_args = (
        f"{arch_flag}"
        f"--fabric-dims={fabric_w},{fabric_h} "
        f"--fabric-offsets=4,1 "
        f"--memcpy --channels=1 "
        f"-o out "
        f"--params={params}"
    )

    print(f"Compiling Picasso CSL for appliance mode:")
    print(f"  CSL dir:     {csl_dir}")
    print(f"  Fabric dims: {fabric_w}x{fabric_h}")
    print(f"  PE grid:     {args.num_cols}x{args.num_rows}")
    print(f"  Params:      {params}")
    print(f"  Hardware:    {args.hardware}")
    print()

    # Launch compile job on the appliance
    with SdkCompiler(disable_version_check=True) as compiler:
        print("Compile job submitted to appliance...")
        artifact_path = compiler.compile(
            csl_dir,           # directory containing CSL files
            layout_file,      # top-level CSL file (selected per --lww-layout)
            compiler_args,    # compiler arguments
            "."               # output directory (on appliance)
        )
        print(f"Compilation successful!")
        print(f"Artifact path: {artifact_path}")

    # Save artifact path for the runner
    # pipelined-LWW kernels are routing_mode=2; otherwise honor --routing-mode
    effective_routing_mode = 2 if args.lww_layout is not None else args.routing_mode
    output_data = {
        "artifact_path": artifact_path,
        "num_cols": args.num_cols,
        "num_rows": args.num_rows,
        "max_local_verts": args.max_local_verts,
        "max_local_edges": args.max_local_edges,
        "max_boundary": args.max_boundary,
        "max_relay": args.max_relay,
        "max_palette_size": args.max_palette_size,
        "max_list_size": args.max_list_size,
        "routing_mode": effective_routing_mode,
        "lww_layout": args.lww_layout,
        "seg_size": args.seg_size,
        "hardware": args.hardware,
    }
    with open(args.output, 'w', encoding='utf8') as f:
        json.dump(output_data, f, indent=2)

    print(f"Artifact info written to: {args.output}")


if __name__ == '__main__':
    main()
