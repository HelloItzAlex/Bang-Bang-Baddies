import time
import queue
import threading

import nidaqmx
from nidaqmx.constants import AcquisitionType, TerminalConfiguration


# -----------------------------
# Config (single place to edit)
# -----------------------------
class DaqGlobals:
    device   = "Dev1"
    rate     = 1000.0      # aggregate samples/sec across all channels
    chunk    = 100         # samples per channel per read
    min_v    = 0.0
    max_v    = 10.0
    terminal = TerminalConfiguration.RSE


# -----------------------------
# Polling reader (A)
# -----------------------------
def daq_reader(stop_event: threading.Event, out_q: queue.Queue) -> None:
    g = DaqGlobals
    chans = f"{g.device}/ai0:7"

    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(
            chans,
            terminal_config=g.terminal,
            min_val=g.min_v,
            max_val=g.max_v,
        )

        task.timing.cfg_samp_clk_timing(
            rate=g.rate,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=g.chunk,  # buffer hint in continuous mode
        )

        # Optional extra slack (uncomment if you see buffer errors)
        # task.in_stream.input_buf_size = 10_000

        print(
            f"Streaming {chans} @ {g.rate} S/s aggregate "
            f"({g.rate/8:.1f} S/s per channel), chunk={g.chunk}"
        )
        print("Ctrl+C to stop.\n")

        task.start()

        while not stop_event.is_set():
            data = task.read(number_of_samples_per_channel=g.chunk)  # [8][chunk]
            ts = time.time()

            # Queue handling: if full, drop oldest to keep running "live"
            try:
                out_q.put((ts, data), timeout=0.1)
            except queue.Full:
                try:
                    out_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    out_q.put_nowait((ts, data))
                except queue.Full:
                    pass


# -----------------------------
# Demo consumer (prints latest)
# -----------------------------
def printer_consumer(stop_event: threading.Event, in_q: queue.Queue) -> None:
    while not stop_event.is_set():
        try:
            ts, data = in_q.get(timeout=0.25)
        except queue.Empty:
            continue

        latest = [ch_samples[-1] for ch_samples in data]
        print(f"{ts:.3f}  " + "  ".join(f"{v:7.3f}V" for v in latest))


# -----------------------------
# Main
# -----------------------------
def main():
    q_data = queue.Queue(maxsize=50)
    stop = threading.Event()

    t_reader = threading.Thread(target=daq_reader, args=(stop, q_data), daemon=True)
    t_print  = threading.Thread(target=printer_consumer, args=(stop, q_data), daemon=True)

    t_reader.start()
    t_print.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop.set()
        t_reader.join(timeout=2.0)
        t_print.join(timeout=2.0)


if __name__ == "__main__":
    main()
