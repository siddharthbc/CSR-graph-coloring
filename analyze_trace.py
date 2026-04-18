#!/usr/bin/env python3
"""Analyze CTF trace packet headers to check simulation progress."""
import struct
import sys

TRACE_FILE = "csl_compiled_out/simfab_traces/stream0"

MAGIC = 0xC1FC1FC1
# packet.header: magic(u32, align 8) + stream_id(u64, align 8) = 16 bytes
# packet.context: packet_size(u64) + content_size(u64) + timestamp_begin(u64) + timestamp_end(u64) + events_discarded(u64) = 40 bytes
# Total = 56 bytes
FULL_HDR = '<I4xQQQQQQ'
HDR_SIZE = struct.calcsize(FULL_HDR)

print(f"Header struct size: {HDR_SIZE} bytes")

packets = []
with open(TRACE_FILE, 'rb') as f:
    file_size = f.seek(0, 2)
    print(f"File size: {file_size:,} bytes ({file_size/1e9:.2f} GB)")
    
    # Read first 20 packets
    f.seek(0)
    offset = 0
    for i in range(20):
        f.seek(offset)
        data = f.read(HDR_SIZE)
        if len(data) < HDR_SIZE:
            break
        vals = struct.unpack(FULL_HDR, data)
        magic, stream_id, pkt_size_bits, content_size_bits, ts_begin, ts_end, events_discard = vals
        if magic != MAGIC:
            print(f"Bad magic at offset {offset}: 0x{magic:08X}")
            break
        pkt_size_bytes = pkt_size_bits // 8
        packets.append({
            'idx': i, 'offset': offset, 'pkt_size': pkt_size_bytes,
            'content_size': content_size_bits // 8,
            'ts_begin': ts_begin, 'ts_end': ts_end,
            'events_discarded': events_discard,
        })
        offset += pkt_size_bytes

    print(f"\n=== First {len(packets)} packets ===")
    for p in packets[:10]:
        print(f"  Pkt {p['idx']}: offset={p['offset']:>15,}  size={p['pkt_size']:>10,}  "
              f"ts=[{p['ts_begin']:>15,} .. {p['ts_end']:>15,}]  discarded={p['events_discarded']}")

    # Compute typical packet size
    if not packets:
        print("No packets found!")
        sys.exit(1)
    
    typical_pkt_size = packets[0]['pkt_size']
    approx_total_pkts = file_size // typical_pkt_size
    print(f"\nTypical packet size: {typical_pkt_size:,} bytes")
    print(f"Approx total packets: {approx_total_pkts:,}")

    # Read last 10 packets
    print(f"\n=== Last packets (from end of file) ===")
    last_pkts = []
    for i in range(min(20, approx_total_pkts)):
        pkt_offset = file_size - (i + 1) * typical_pkt_size
        if pkt_offset < 0:
            break
        f.seek(pkt_offset)
        data = f.read(HDR_SIZE)
        if len(data) < HDR_SIZE:
            break
        vals = struct.unpack(FULL_HDR, data)
        magic = vals[0]
        if magic == MAGIC:
            _, stream_id, pkt_size_bits, content_size_bits, ts_begin, ts_end, events_discard = vals
            last_pkts.append({
                'offset': pkt_offset, 'pkt_size': pkt_size_bits // 8,
                'ts_begin': ts_begin, 'ts_end': ts_end,
                'events_discarded': events_discard,
            })
    
    last_pkts.reverse()
    for p in last_pkts[-10:]:
        print(f"  offset={p['offset']:>15,}  size={p['pkt_size']:>10,}  "
              f"ts=[{p['ts_begin']:>15,} .. {p['ts_end']:>15,}]  discarded={p['events_discarded']}")

    # Also sample a few packets from the middle
    print(f"\n=== Middle packets (sampled) ===")
    mid_offsets = [file_size * frac for frac in [0.25, 0.5, 0.75]]
    for target in mid_offsets:
        # Align to packet boundary
        pkt_idx = int(target // typical_pkt_size)
        pkt_offset = pkt_idx * typical_pkt_size
        f.seek(pkt_offset)
        data = f.read(HDR_SIZE)
        if len(data) < HDR_SIZE:
            continue
        vals = struct.unpack(FULL_HDR, data)
        if vals[0] == MAGIC:
            _, stream_id, pkt_size_bits, content_size_bits, ts_begin, ts_end, events_discard = vals
            pct = pkt_offset / file_size * 100
            print(f"  @{pct:5.1f}%: offset={pkt_offset:>15,}  "
                  f"ts=[{ts_begin:>15,} .. {ts_end:>15,}]  discarded={events_discard}")

    # Summary
    if packets and last_pkts:
        first_ts = packets[0]['ts_begin']
        last_ts = last_pkts[-1]['ts_end']
        total_span = last_ts - first_ts
        print(f"\n{'='*60}")
        print(f"SUMMARY:")
        print(f"  First timestamp:  {first_ts:>15,}")
        print(f"  Last timestamp:   {last_ts:>15,}")
        print(f"  Total time span:  {total_span:>15,} ticks")
        print(f"  Total packets:    ~{approx_total_pkts:,}")
        
        # Check if timestamps are monotonically increasing across the file
        # (indicating continuous progress vs early stall)
        if last_ts > first_ts:
            progress_pct = (last_ts - first_ts) / max(last_ts, 1) * 100
            print(f"\n  VERDICT: Simulation was ACTIVE throughout the trace span")
            print(f"  Time covered: {total_span:,} ticks")
        else:
            print(f"\n  VERDICT: Timestamps not increasing — possible early stall")
