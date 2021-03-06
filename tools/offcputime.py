#!/usr/bin/python
#
# offcputime    Summarize off-CPU time by kernel stack trace
#               For Linux, uses BCC, eBPF.
#
# USAGE: offcputime [-h] [-u] [-p PID] [-v] [-f] [duration]
#
# The current implementation uses an unrolled loop for x86_64, and was written
# as a proof of concept. This implementation should be replaced in the future
# with an appropriate bpf_ call, when available.
#
# Copyright 2016 Netflix, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 13-Jan-2016	Brendan Gregg	Created this.

from __future__ import print_function
from bcc import BPF
from time import sleep, strftime
import argparse
import signal

# arguments
examples = """examples:
    ./offcputime             # trace off-CPU stack time until Ctrl-C
    ./offcputime 5           # trace for 5 seconds only
    ./offcputime -f 5        # 5 seconds, and output in folded format
    ./offcputime -u          # don't include kernel threads (user only)
    ./offcputime -p 185      # trace fo PID 185 only
"""
parser = argparse.ArgumentParser(
    description="Summarize off-CPU time by kernel stack trace",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-u", "--useronly", action="store_true",
    help="user threads only (no kernel threads)")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
parser.add_argument("-v", "--verbose", action="store_true",
    help="show raw addresses")
parser.add_argument("-f", "--folded", action="store_true",
    help="output folded format")
parser.add_argument("duration", nargs="?", default=99999999,
    help="duration of trace, in seconds")
args = parser.parse_args()
folded = args.folded
duration = int(args.duration)
debug = 0
if args.pid and args.useronly:
    print("ERROR: use either -p or -u.")
    exit()

# signal handler
def signal_ignore(signal, frame):
    print()

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>

#define MINBLOCK_US	1

struct key_t {
    char name[TASK_COMM_LEN];
    int stack_id;
};
BPF_HASH(counts, struct key_t);
BPF_HASH(start, u32);
BPF_STACK_TRACE(stack_traces, 1024)

int oncpu(struct pt_regs *ctx, struct task_struct *prev) {
    u32 pid;
    u64 ts, *tsp;

    // record previous thread sleep time
    if (FILTER) {
        pid = prev->pid;
        ts = bpf_ktime_get_ns();
        start.update(&pid, &ts);
    }

    // calculate current thread's delta time
    pid = bpf_get_current_pid_tgid();
    tsp = start.lookup(&pid);
    if (tsp == 0)
        return 0;        // missed start or filtered
    u64 delta = bpf_ktime_get_ns() - *tsp;
    start.delete(&pid);
    delta = delta / 1000;
    if (delta < MINBLOCK_US)
        return 0;

    // create map key
    u64 zero = 0, *val;
    struct key_t key = {};

    bpf_get_current_comm(&key.name, sizeof(key.name));
    key.stack_id = stack_traces.get_stackid(ctx, BPF_F_REUSE_STACKID);

    val = counts.lookup_or_init(&key, &zero);
    (*val) += delta;
    return 0;
}
"""
if args.pid:
    filter = 'pid == %s' % args.pid
elif args.useronly:
    filter = '!(prev->flags & PF_KTHREAD)'
else:
    filter = '1'
bpf_text = bpf_text.replace('FILTER', filter)
if debug:
    print(bpf_text)

# initialize BPF
b = BPF(text=bpf_text)
b.attach_kprobe(event="finish_task_switch", fn_name="oncpu")
matched = b.num_open_kprobes()
if matched == 0:
    print("0 functions traced. Exiting.")
    exit()

# header
if not folded:
    print("Tracing off-CPU time (us) by kernel stack", end="")
    if duration < 99999999:
        print(" for %d secs." % duration)
    else:
        print("... Hit Ctrl-C to end.")

# output
while (1):
    try:
        sleep(duration)
    except KeyboardInterrupt:
        # as cleanup can take many seconds, trap Ctrl-C:
        signal.signal(signal.SIGINT, signal_ignore)

    if not folded:
        print()
    counts = b.get_table("counts")
    stack_traces = b.get_table("stack_traces")
    for k, v in sorted(counts.items(), key=lambda counts: counts[1].value):
        if folded:
            # print folded stack output
            stack = list(stack_traces.walk(k.stack_id))[1:]
            line = [k.name.decode()] + [b.ksym(addr) for addr in reversed(stack)]
            print("%s %d" % (";".join(line), v.value))
        else:
            # print default multi-line stack output
            for addr in stack_traces.walk(k.stack_id):
                print("    %-16x %s" % (addr, b.ksym(addr)))
            print("    %-16s %s" % ("-", k.name))
            print("        %d\n" % v.value)
    counts.clear()

    if not folded:
        print("Detaching...")
    exit()
