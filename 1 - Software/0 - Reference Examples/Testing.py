import threading
import nidaqmx
from nidaqmx import stream_readers
from nidaqmx.constants import TerminalConfiguration, AcquisitionType, WAIT_INFINITELY
#from nidaqmx.constants import LineGrouping
#import nidaqmx.system
import numpy as np
import time
#import csv
#import os
#from queue import Queue, Empty, Full
import synnax as sy
#import keyboard

# Constants Setup
numAINChannel = 8  # Number of AIN channels
numDIOChannels = 4 # Number of DIO channels

# Synnax Setup
client = sy.Synnax(
    host="localhost",
    port=9090,
    username="synnax",
    password="seldon",
    secure=False
)




# AIN Reader -------------------------------
def ain_reader(stop_event, ain_queue, continuity_queue, csv_queue):
    fs_acq = 1000  # Acquisition frequency (Hz)
    AIN_str = "Dev1/ai0:7"  # Voltage module
    update_interval = 100 # Read 100 samples per callback = .1s chunks

    try:
        with nidaqmx.Task() as task:
            # Add AIN channels
            task.ai_channels.add_ai_voltage_chan(
                AIN_str,
                terminal_config=TerminalConfiguration.RSE,
                min_val=-10, max_val=10
            )

            task.timing.cfg_samp_clk_timing(rate=fs_acq, sample_mode=AcquisitionType.CONTINUOUS)
            task.in_stream.input_buf_size = fs_acq * 5
            task.in_stream.overwrite = nidaqmx.constants.OverwriteMode.OVERWRITE_UNREAD_SAMPLES

            reader = stream_readers.AnalogMultiChannelReader(task.in_stream)
            num_channels = len(task.ai_channels)

            def callback(task_idx, event_type, num_samples, cb_data=None):
                try:
                    buffer = np.zeros((num_channels, num_samples), dtype=np.float64)
                    reader.read_many_sample(buffer, num_samples, timeout=WAIT_INFINITELY)
                    data = buffer.T

                    if len(data) == 0:
                        return 0

                    print(data[1])

                except Exception as e:
                    print(f"PT callback error: {e}")
                    return -1
                return 0

            task.register_every_n_samples_acquired_into_buffer_event(update_interval, callback)
            task.start()
            print("PT thread running (1000 Hz logging, 50 Hz display)")

            while not stop_event.is_set():
                time.sleep(0.1)

    except Exception as e:
        print(f"PT reader error: {e}")
    finally:
        print("PT thread stopping")

if __name__ == "__main__":
    stop_event = threading.Event()
    
    # Create queues
    pt_queue = Queue(maxsize=1000)  # Increased from 500 to 1000
    tc_queue = Queue(maxsize=200)
    lc_queue = Queue(maxsize=500)
    csv_queue = Queue(maxsize=5000)
    continuity_queue = Queue(maxsize=500)
    
    # Create threads
    thread_configs = [
        ("AIN Reader", ain_reader, (stop_event, pt_queue, continuity_queue, csv_queue)),
        ("CSV Writer", csv_writer, (stop_event, csv_queue)),
        ("PT Synnax", synnax_pt_writer_robust, (stop_event, pt_queue, client, pt_time_channel, pt_raw_channels, pt_scaled_channels, PT_SCALING)),
        ("Valve Module B", module_b_worker, (stop_event, continuity_queue)),
    ]
    
    threads = []
    for name, target, args in thread_configs:
        try:
            thread = threading.Thread(target=target, args=args, name=name)
            thread.daemon = True
            threads.append((name, thread))
            thread.start()
            print(f"Started {name} thread")
        except Exception as e:
            print(f"Failed to start {name} thread: {e}")
    
    try:
        print("\nROCKET DAQ SYSTEM STARTED")
        print("=" * 60)
        print(f"PT: {num_Pt_chan} channels, 1000Hz → 50Hz display + CSV logging")
        print("PT Scaling loaded:")
        for i in range(min(5, len(PT_SCALING))):
            mult, offset = PT_SCALING[i]
            name = PT_NAMES.get(i, f"PT_{i}")
            if offset == 0:
                print(f"  {name}: volts × {mult}")
            else:
                print(f"  {name}: volts × {mult} + {offset}")
        if len(PT_SCALING) > 5:
            print(f"  ... and {len(PT_SCALING)-5} more channels")
        print(f"TC: {num_Tc_chan} channels, 13Hz → direct display + CSV logging")  
        print(f"LC: {num_Lc_chan} channels, 1000Hz → 100Hz display + CSV logging")
        print("Raw NI data logged to CSV files")
        print("Improved buffer sizes and queue management")
        print("=" * 60)
        print("Press Ctrl+C to stop...")
        
        while True:
            time.sleep(10)
            dead_threads = [(name, t) for name, t in threads if not t.is_alive()]
            if dead_threads:
                print(f"WARNING: Dead threads detected: {[name for name, _ in dead_threads]}")
            
    except KeyboardInterrupt:
        print("\nStopping rocket DAQ system...")
        stop_event.set()
        
        for name, thread in threads:
            thread.join(timeout=5)
            if thread.is_alive():
                print(f"WARNING: {name} thread did not stop cleanly")
            else:
                print(f"{name} thread stopped")
            
        print("All systems stopped safely")