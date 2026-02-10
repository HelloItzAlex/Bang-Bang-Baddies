#Set-Location -Path "C:\Users\SDSURP"
#synnax start --listen=localhost:9090 --insecure  --license-key=155085-64318284-7402568864
import threading
import nidaqmx
from nidaqmx import stream_readers
from nidaqmx.constants import TerminalConfiguration, AcquisitionType, WAIT_INFINITELY
from nidaqmx.constants import LineGrouping
import nidaqmx.system
import numpy as np
import time
import csv
import os
from queue import Queue, Empty, Full
import synnax as sy
import keyboard


CSV_OUTPUT_DIR = r"C:\RP" #csv logs
os.makedirs(CSV_OUTPUT_DIR, exist_ok=True)


# Constants Setup
num_Pt_chan = 40  # Number of PT channels (32 voltage + 8 current)
num_Tc_chan = 16   # Number of TC channels
num_Lc_chan = 2   # Number of LC channels
numRelayChannels = 8

# Synnax Setup
client = sy.Synnax(
    host="localhost",
    port=9090,
    username="synnax",
    password="seldon",
    secure=False
)


# PT scaling factors + name
def load_pt_scaling():
    """Load PT scaling factors and channel names from CSV file"""
    scaling_factors = {}
    channel_names = {}  # New dict for names
    scaling_file = r"C:\Users\kiril\Downloads\ScalingDefsPTs3.csv"
    
    if not os.path.exists(scaling_file):
        print(f"Error: Scaling file {scaling_file} not found, using default scaling and names")
        return {i: (100.0, 0.0) for i in range(num_Pt_chan)}, {i: f"PT_{i}" for i in range(num_Pt_chan)}
    
    try:
        with open(scaling_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"Error: Scaling file {scaling_file} is empty or invalid")
                return {i: (100.0, 0.0) for i in range(num_Pt_chan)}, {i: f"PT_{i}" for i in range(num_Pt_chan)}
            
            expected_fields = ['Sensor Physical Channel', 'Scaling Equation', 'Channel Names']
            if not all(field in reader.fieldnames for field in expected_fields):
                print(f"Error: Scaling file missing required columns: {expected_fields}")
                return {i: (100.0, 0.0) for i in range(num_Pt_chan)}, {i: f"PT_{i}" for i in range(num_Pt_chan)}
            
            for row in reader:
                channel = row['Sensor Physical Channel'].strip()
                equation = row['Scaling Equation'].strip()
                name = row['Channel Names'].strip() if 'Channel Names' in row else f"PT_unknown"
                print(f"Processing scaling entry: {channel}, {equation}, {name}")
                
                if '/ai' not in channel:
                    print(f"Skipping invalid channel: {channel}")
                    continue
                
                parts = channel.split('/')
                module = parts[0].lower()
                ai_num = int(parts[1].split('ai')[1])
                
                if module == 'cdaq1mod1':
                    chan_num = ai_num  # 0-31 for voltage
                elif module == 'cdaq1mod3':
                    chan_num = 32 + ai_num  # 32-39 for current
                else:
                    print(f"Skipping unrecognized module: {module}")
                    continue
                
                # Parse equation
                multiplier = 1.0
                offset = 0.0
                
                eq = equation.replace('x', '').strip()
                try:
                    if '+' in eq or '-' in eq:
                        import re
                        parts = re.split(r'([+-])', eq)
                        multiplier_part = parts[0].strip()
                        if len(parts) > 1:
                            sign = parts[1]
                            offset_part = parts[2].strip()
                            offset = float(sign + offset_part)
                    else:
                        multiplier_part = eq
                        offset = 0.0
                    
                    if '*' in multiplier_part:
                        multiplier_part = multiplier_part.split('*')[1].strip()
                    if '/' in multiplier_part:
                        try:
                            multiplier = float(eval(multiplier_part, {"__builtins__": {}}))
                        except Exception as e:
                            print(f"Error parsing multiplier '{multiplier_part}' for {channel}: {e}")
                            continue
                    else:
                        multiplier = float(multiplier_part)
                
                except Exception as e:
                    print(f"Skipping invalid equation for {channel}: {equation} (Error: {e})")
                    continue
                
                scaling_factors[chan_num] = (multiplier, offset)
                channel_names[chan_num] = name if name else f"PT_{chan_num}"
        
        print(f"Loaded scaling factors and names for {len(scaling_factors)} PT channels")
        # Fill missing with defaults
        for i in range(num_Pt_chan):
            if i not in scaling_factors:
                scaling_factors[i] = (100.0, 0.0)
                channel_names[i] = f"PT_{i}"
                print(f"Default scaling and name applied for PT_{i}: (100.0, 0.0), PT_{i}")
        return scaling_factors, channel_names
        
    except Exception as e:
        print(f"Error loading scaling file {scaling_file}: {e}")
        return {i: (100.0, 0.0) for i in range(num_Pt_chan)}, {i: f"PT_{i}" for i in range(num_Pt_chan)}

# Load scaling factors at startup
PT_SCALING, PT_NAMES = load_pt_scaling()


# Synnax writer start-time helpers (unchanged)

def get_safe_start_timestamp(client, time_channel, sensor_type=""):
    """
    Get a timestamp that won't conflict with existing data.
    """
    try:
        end_time = sy.TimeStamp.now()
        start_time = end_time - sy.TimeSpan.HOUR * 48
        
        try:
            with client.open_streamer([time_channel.key]) as streamer:
                frame = streamer.read(timeout=1.0)
                
                if frame is not None and len(frame) > 0 and time_channel.key in frame:
                    data = frame[time_channel.key]
                    if len(data) > 0:
                        latest_timestamp = data[-1]
                        safe_start = latest_timestamp + sy.TimeSpan.SECOND * 10
                        print(f"{sensor_type} writer: Found existing data, starting from {safe_start}")
                        return safe_start
                    else:
                        print(f"{sensor_type} writer: No data in frame, starting from now + buffer")
                        return sy.TimeStamp.now() + sy.TimeSpan.SECOND * 5
                else:
                    print(f"{sensor_type} writer: No existing data found, starting from now")
                    return sy.TimeStamp.now() + sy.TimeSpan.SECOND * 2
                    
        except Exception as query_error:
            print(f"{sensor_type} writer: Could not query existing data ({query_error}), using safe fallback")
            return sy.TimeStamp.now() + sy.TimeSpan.MINUTE * 1
            
    except Exception as e:
        print(f"{sensor_type} writer: Error in timestamp calculation ({e}), using very safe fallback")
        return sy.TimeStamp.now() + sy.TimeSpan.MINUTE * 2

def create_robust_writer_session(client, channels, sensor_type, max_retries=5):
    """
    Create a writer session with robust error handling and timestamp management.
    """
    for attempt in range(max_retries):
        try:
            time_channel = channels[0]
            base_timestamp = get_safe_start_timestamp(client, time_channel, sensor_type)
            writer = client.open_writer(base_timestamp, [ch.key for ch in channels], enable_auto_commit=True)
            print(f"{sensor_type} writer session created successfully (attempt {attempt + 1})")
            return writer, base_timestamp
        except Exception as e:
            print(f"{sensor_type} writer creation failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 1.0
                print(f"{sensor_type} retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                raise Exception(f"Failed to create {sensor_type} writer after {max_retries} attempts")


# Synnax channel creation

pt_time_channel = client.channels.create(
    name="pt_channels_time",
    is_index=True,
    data_type=sy.DataType.TIMESTAMP,
    retrieve_if_name_exists=True,
)
tc_time_channel = client.channels.create(
    name="tc_channels_time",
    is_index=True,
    data_type=sy.DataType.TIMESTAMP,
    retrieve_if_name_exists=True,
)
lc_time_channel = client.channels.create(
    name="lc_channels_time",
    is_index=True,
    data_type=sy.DataType.TIMESTAMP,
    retrieve_if_name_exists=True,
)

# PT raw + scaled
pt_raw_channels = []
for i in range(num_Pt_chan):
    base_name = PT_NAMES.get(i, f"PT_{i}")
    channel = client.channels.create(
        name=base_name,
        index=pt_time_channel.key,
        data_type=sy.DataType.FLOAT32,
        retrieve_if_name_exists=True,
    )
    pt_raw_channels.append(channel)

pt_scaled_channels = []
for i in range(num_Pt_chan):
    base_name = PT_NAMES.get(i, f"PT_{i}")
    channel = client.channels.create(
        name=f"{base_name}_scaled",
        index=pt_time_channel.key,
        data_type=sy.DataType.FLOAT32,
        retrieve_if_name_exists=True,
    )
    pt_scaled_channels.append(channel)

# TC channels
tc_channels = []
for i in range(num_Tc_chan):
    channel = client.channels.create(
        name=f"tc_{i}_celsius",
        index=tc_time_channel.key,
        data_type=sy.DataType.FLOAT32,
        retrieve_if_name_exists=True,
    )
    tc_channels.append(channel)

# LC channels
lc_channels = []
for i in range(num_Lc_chan):
    channel = client.channels.create(
        name=f"lc_{i}_lbf",
        index=lc_time_channel.key,
        data_type=sy.DataType.FLOAT32,
        retrieve_if_name_exists=True,
    )
    lc_channels.append(channel)


# Logging enable channels

log_enable_time = client.channels.create(
    name="LOG_ENABLE_TIME", 
    is_index=True, 
    data_type=sy.DataType.TIMESTAMP, 
    retrieve_if_name_exists=True,
)
log_enable_cmd = client.channels.create(
    name="LOG_ENABLE_CMD", 
    data_type=sy.DataType.UINT8, 
    index=log_enable_time.key, 
    retrieve_if_name_exists=True,
)
log_enable_state_time = client.channels.create(
    name="LOG_ENABLE_STATE_TIME", 
    is_index=True, 
    data_type=sy.DataType.TIMESTAMP, 
    retrieve_if_name_exists=True,
)
log_enable_state = client.channels.create(
    name="LOG_ENABLE_STATE", 
    data_type=sy.DataType.UINT8, 
    index=log_enable_state_time.key, 
    retrieve_if_name_exists=True,
)

# in-memory flag controlled by GUI
logging_enabled_flag = {"val": 0}


# Logging controller (with ROTATE)

def logging_controller(stop_event, csv_queue):
    """Listens to LOG_ENABLE_CMD and mirrors into LOG_ENABLE_STATE + in-memory flag.
       On rising edge (0->1) it requests CSV rotation."""
    try:
        with client.open_streamer([log_enable_cmd.name]) as streamer, \
             client.open_writer(start=sy.TimeStamp.now(),
                                channels=[log_enable_state_time, log_enable_state],
                                enable_auto_commit=True) as writer:
            last = None
            while not stop_event.is_set():
                frame = streamer.read(timeout=0.5)
                if frame is None:
                    continue
                vals = frame.get(log_enable_cmd.name)
                if vals is None or len(vals) == 0:
                    continue

                new_val = int(vals[-1]) & 1
                if new_val != last:
                    last = new_val
                    logging_enabled_flag["val"] = new_val
                    now = sy.TimeStamp.now()
                    writer.write({
                        log_enable_state_time.key: now,
                        log_enable_state.key: new_val
                    })
                    print(f"[LOG] Logging {'ENABLED' if new_val else 'DISABLED'}")

                    # Rising edge => rotate CSV files
                    if new_val == 1:
                        try:
                            csv_queue.put_nowait({"kind": "ROTATE"})
                        except Exception as e:
                            print(f"[LOG] Could not enqueue ROTATE: {e}")

    except Exception as e:
        print(f"logging_controller error: {e}")
    finally:
        try:
            with client.open_writer(start=sy.TimeStamp.now(),
                                    channels=[log_enable_state_time, log_enable_state],
                                    enable_auto_commit=True) as writer:
                now = sy.TimeStamp.now()
                writer.write({log_enable_state_time.key: now, log_enable_state.key: 0})
        except:
            pass
        logging_enabled_flag["val"] = 0
        print("[LOG] Controller stopped (state forced OFF)")


# CSV writer with rotation

def csv_writer(stop_event, csv_queue):
    # Headers
    pt_headers = ["unix_time_ns"] + [f"pt_raw_{i}" for i in range(num_Pt_chan)] + [f"pt_psi_{i}" for i in range(num_Pt_chan)]
    tc_headers = ["unix_time_s"] + [f"tc_{i}_c" for i in range(num_Tc_chan)]
    lc_headers = ["unix_time_s"] + [f"lc_{i}_lbf" for i in range(num_Lc_chan)]

    def open_new_file_set():
        """Open a new set of timestamped CSVs and writers, write headers, return dicts."""
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        pt_name = os.path.join(CSV_OUTPUT_DIR, f"pt_log_{ts_str}.csv")  #csv logs
        tc_name = os.path.join(CSV_OUTPUT_DIR, f"tc_log_{ts_str}.csv")
        lc_name = os.path.join(CSV_OUTPUT_DIR, f"lc_log_{ts_str}.csv")


        files = {
            "PT": open(pt_name, 'a', newline='', buffering=1<<16),
            "TC": open(tc_name, 'a', newline='', buffering=1<<15),
            "LC": open(lc_name, 'a', newline='', buffering=1<<15),
        }
        writers = {k: csv.writer(v) for k, v in files.items()}

        # Write headers
        writers["PT"].writerow(pt_headers)
        writers["TC"].writerow(tc_headers)
        writers["LC"].writerow(lc_headers)

        print(f"[CSV] Started new log files at {ts_str}: {pt_name}, {tc_name}, {lc_name}")
        return files, writers

    files = {}
    writers = {}
    flush_counter = 0

    try:
        # Start with an initial timestamped set
        files, writers = open_new_file_set()
        print("CSV writer started - logging raw NI data with proper timing")

        while not stop_event.is_set():
            try:
                item = csv_queue.get(timeout=1.0)
            except Empty:
                continue

            # Handle rotation request
            if item.get("kind") == "ROTATE":
                for fh in files.values():
                    try:
                        fh.close()
                    except:
                        pass
                files, writers = open_new_file_set()
                flush_counter = 0
                continue

            # Normal data
            kind = item.get('kind')  # 'PT', 'TC', or 'LC'
            if kind not in writers:
                print(f"CSV: unknown kind {kind}")
                continue

            try:
                if kind == 'PT':
                    ts0 = item['time_ns']  # base timestamp (ns) at start of the block
                    for i, row in enumerate(item['rows']):
                        ts = ts0 + i * 1_000_000  # +1 ms per sample @ 1000 Hz
                        writers[kind].writerow([ts] + row)

                elif kind == 'TC':
                    ts0 = int(item['time_s'] * 1e9)
                    for i, row in enumerate(item['rows']):
                        # 5 Hz => 200 ms per sample
                        ts = ts0 + i * 200_000_000
                        writers[kind].writerow([ts] + row)

                elif kind == 'LC':
                    ts0 = int(item['time_s'] * 1e9)
                    for i, row in enumerate(item['rows']):
                        # 1000 Hz => 1 ms per sample
                        ts = ts0 + i * 1_000_000
                        writers[kind].writerow([ts] + row)

                flush_counter += 1
                if flush_counter % 50 == 0:
                    for f in files.values():
                        f.flush()

            except Exception as e:
                print(f"CSV writer row error: {e}")
                continue

    except Exception as e:
        print(f"CSV writer setup error: {e}")
    finally:
        for fh in files.values():
            try:
                fh.close()
            except:
                pass
        print("CSV writer stopped")


# Synnax writers (unchanged logic)

def synnax_pt_writer_robust(stop_event, pt_queue, client, time_channel, raw_channels, scaled_channels, scaling_factors):
    try:
        writer, start_time = create_robust_writer_session(client, [time_channel] + raw_channels + scaled_channels, "PT")
        print("PT writer: Connected successfully, beginning data acquisition")
        
        while not stop_event.is_set():
            try:
                data_packet = pt_queue.get(timeout=1.0)
                if data_packet['sensor_type'] != 'PT':
                    continue
                
                timestamp = data_packet['time']  # sy.TimeStamp (nanoseconds)
                data = data_packet['data']
                if data.shape[0] == 0:
                    continue
                
                latest_data = data[-1, :]  # Shape: (40,)
                
                frame = {time_channel.key: np.array([int(timestamp)], dtype=np.int64)}
                
                for i, channel in enumerate(raw_channels):
                    frame[channel.key] = np.array([latest_data[i]], dtype=np.float32)
                
                for i, channel in enumerate(scaled_channels):
                    multiplier, offset = scaling_factors[i]
                    scaled_value = latest_data[i] * multiplier + offset
                    frame[channel.key] = np.array([scaled_value], dtype=np.float32)
                
                writer.write(frame)
            except Empty:
                continue
            except Exception as e:
                print(f"PT write error: {e}")
                time.sleep(0.1)
    except Exception as e:
        print(f"PT writer error: {e}")
    finally:
        print("PT writer stopping")

def synnax_tc_writer_robust(stop_event, tc_queue, client, tc_time_channel, tc_channels):
    print("TC Synnax writer started (ROBUST MODE)")
    PERIOD_US_NUM = 1_000_000
    PERIOD_US_DEN = 13
    sample_counter = 0
    consecutive_errors = 0
    max_consecutive_errors = 10
    write_channels = [tc_time_channel] + tc_channels
    
    while not stop_event.is_set():
        writer = None
        base_timestamp = None
        accum_us = 0
        
        try:
            writer, base_timestamp = create_robust_writer_session(
                client, write_channels, "TC", max_retries=5
            )
            consecutive_errors = 0
            print("TC writer: Connected successfully, beginning data acquisition")
            
            with writer:
                while not stop_event.is_set():
                    try:
                        batch = tc_queue.get(timeout=2.0)
                        if batch['sensor_type'] != 'TC':
                            continue
                        data = batch['data']
                        for sample in data:
                            accum_us += PERIOD_US_NUM // PERIOD_US_DEN
                            rem = (PERIOD_US_NUM % PERIOD_US_DEN)
                            if rem:
                                if (sample_counter % PERIOD_US_DEN) < rem:
                                    accum_us += 1
                            sample_timestamp = base_timestamp + sy.TimeSpan.MICROSECOND * accum_us
                            
                            synnax_data = {tc_time_channel.key: sample_timestamp}
                            for j, temp_val in enumerate(sample):
                                if j < len(tc_channels):
                                    synnax_data[tc_channels[j].key] = float(temp_val)
                            writer.write(synnax_data)
                            sample_counter += 1
                        consecutive_errors = 0
                    except Exception as write_error:
                        consecutive_errors += 1
                        print(f"TC write error #{consecutive_errors}: {write_error}")
                        if consecutive_errors >= max_consecutive_errors:
                            print(f"TC writer: Too many consecutive errors, recreating connection")
                            break
                        time.sleep(0.1)
        except Exception as connection_error:
            consecutive_errors += 1
            print(f"TC connection error #{consecutive_errors}: {connection_error}")
            if consecutive_errors < max_consecutive_errors:
                wait_time = min(30, (2 ** min(consecutive_errors - 1, 5)))
                print(f"TC writer: Retrying connection in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print("TC writer: Maximum error threshold reached, stopping writer")
                break
        finally:
            if writer:
                try: writer.close()
                except: pass
    print("TC Synnax writer stopped")

def synnax_lc_writer_robust(stop_event, lc_queue, client, lc_time_channel, lc_channels):
    print("LC Synnax writer started (ROBUST MODE)")
    STEP_US = 10_000  # 10Hz display rate
    sample_counter = 0
    display_counter = 0
    consecutive_errors = 0
    max_consecutive_errors = 10
    DECIMATION_FACTOR = 10
    write_channels = [lc_time_channel] + lc_channels
    
    while not stop_event.is_set():
        writer = None
        base_timestamp = None
        
        try:
            writer, base_timestamp = create_robust_writer_session(
                client, write_channels, "LC", max_retries=5
            )
            consecutive_errors = 0
            print("LC writer: Connected successfully, beginning data acquisition")
            
            with writer:
                while not stop_event.is_set():
                    try:
                        batch_data = lc_queue.get(timeout=2.0)
                        if batch_data['sensor_type'] != 'LC':
                            continue
                        data = batch_data['data']
                        for sample in data:
                            if sample_counter % DECIMATION_FACTOR == 0:
                                sample_timestamp = base_timestamp + sy.TimeSpan.MICROSECOND * (sample_counter * STEP_US)
                                synnax_data = {lc_time_channel.key: sample_timestamp}
                                for j, force_val in enumerate(sample):
                                    if j < len(lc_channels):
                                        synnax_data[lc_channels[j].key] = float(force_val)
                                writer.write(synnax_data)
                                display_counter += 1
                            sample_counter += 1
                        consecutive_errors = 0
                    except Exception as write_error:
                        consecutive_errors += 1
                        print(f"LC write error #{consecutive_errors}: {write_error}")
                        if consecutive_errors >= max_consecutive_errors:
                            print(f"LC writer: Too many consecutive errors ({consecutive_errors}), recreating connection")
                            break
                        time.sleep(0.1)
        except Exception as connection_error:
            consecutive_errors += 1
            print(f"LC connection error #{consecutive_errors}: {connection_error}")
            if consecutive_errors < max_consecutive_errors:
                wait_time = min(30, (2 ** min(consecutive_errors - 1, 5)))
                print(f"LC writer: Retrying connection in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print("LC writer: Maximum error threshold reached, stopping writer")
                break
        finally:
            if writer:
                try: writer.close()
                except: pass
    print("LC Synnax writer stopped")


# Health monitor

def telemetry_health_monitor(stop_event, *queues):
    print("Telemetry health monitor started")
    queue_names = ['PT', 'TC', 'LC', 'CSV']
    alert_thresholds = {
        'PT': 400,
        'TC': 150,
        'LC': 400,
        'CSV': 4000
    }
    while not stop_event.is_set():
        try:
            time.sleep(5)
            current_time = time.strftime("%H:%M:%S")
            pieces = []
            alerts = []
            for i, queue in enumerate(queues):
                name = queue_names[i] if i < len(queue_names) else f"Q{i}"
                size = queue.qsize()
                pieces.append(f"{name}:{size}")
                if size > alert_thresholds.get(name, 1000):
                    alerts.append(f"⚠️  {name} QUEUE CRITICAL: {size}")
            print(f"[{current_time}] Queues: {' | '.join(pieces)}")
            for a in alerts:
                print(a)
        except Exception as e:
            print(f"Health monitor error: {e}")
            time.sleep(1)
    print("Telemetry health monitor stopped")


# NI Readers

def pt_reader(stop_event, pt_queue, continuity_queue, csv_queue):
    fs_acq = 1000
    voltage_str = "cDAQ1Mod1/ai0:31"
    current_str = "cDAQ1Mod3/ai0:7"
    update_interval = 1000  # 1 s chunks

    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                voltage_str,
                terminal_config=TerminalConfiguration.RSE,
                min_val=-10, max_val=10
            )
            task.ai_channels.add_ai_current_chan(
                current_str,
                terminal_config=TerminalConfiguration.RSE,
                min_val=-0.02, max_val=0.02,
                units=nidaqmx.constants.CurrentUnits.AMPS
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

                    batch_start_time = sy.TimeStamp.now()

                    #  LOGGING at 1000 Hz 
                    if logging_enabled_flag["val"] == 1:
                        rows = []
                        for sample in data:
                            raw_vals = sample.tolist()
                            scaled_vals = []
                            for i in range(num_Pt_chan):
                                mult, off = PT_SCALING[i]
                                scaled_vals.append(raw_vals[i] * mult + off)
                            rows.append(raw_vals + scaled_vals)
                        try:
                            csv_queue.put_nowait({
                                'time_ns': int(batch_start_time),
                                'rows': rows,
                                'kind': 'PT'
                            })
                        except Full:
                            pass

                    # Downsample for display only (50 Hz)
                    display_data = data[::20]
                    try:
                        pt_queue.put_nowait({
                            'time': batch_start_time,
                            'data': display_data.copy(),
                            'sensor_type': 'PT'
                        })
                    except Full:
                        pass

                    # Continuity check: last PT31 value
                    latest_pt31 = data[-1, 31]
                    if continuity_queue.qsize() < continuity_queue.maxsize - 10:
                        continuity_queue.put_nowait(latest_pt31)

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

def tc_reader(stop_event, tc_queue, csv_queue):
    fs_acq = 5
    channel_str = "cDAQ1Mod8/ai0:15"

    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_thrmcpl_chan(
                physical_channel=channel_str,
                min_val=-200, max_val=1260,
                thermocouple_type=nidaqmx.constants.ThermocoupleType.K,
                cjc_source=nidaqmx.constants.CJCSource.BUILT_IN
            )
            task.timing.cfg_samp_clk_timing(rate=fs_acq, sample_mode=AcquisitionType.CONTINUOUS)
            task.in_stream.input_buf_size = fs_acq * 20
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

                    batch_start_time = time.time()

                    # LOGGING at 5 Hz 
                    if logging_enabled_flag["val"] == 1:
                        rows = [s.tolist() for s in data]
                        try:
                            csv_queue.put_nowait({
                                'time_s': batch_start_time,
                                'rows': rows,
                                'kind': 'TC'
                            })
                        except Full:
                            pass

                    # Queue for display
                    try:
                        tc_queue.put_nowait({
                            'time': batch_start_time,
                            'data': data.copy(),
                            'sensor_type': 'TC'
                        })
                    except Full:
                        pass

                except Exception as e:
                    print(f"TC callback error: {e}")
                    return -1
                return 0

            task.register_every_n_samples_acquired_into_buffer_event(5, callback)
            task.start()
            print("TC thread running (5 Hz logging)")

            while not stop_event.is_set():
                time.sleep(0.1)

    except Exception as e:
        print(f"TC reader error: {e}")
    finally:
        print("TC thread stopping")

def lc_reader(stop_event, lc_queue, csv_queue):
    fs_acq = 1000
    update_interval = 1000  # 1 s chunks

    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_force_bridge_table_chan(
                "cDAQ1Mod4/ai0:1",
                min_val=0,
                max_val=2000,
                voltage_excit_val=10,
                nominal_bridge_resistance=700,
                electrical_vals=[0, -0.3710, -0.7418, -1.0200, -1.3909, -1.8547],
                physical_vals=[0, 400, 800, 1100, 1500, 2000]
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

                    batch_start_time = time.time()

                    # LOGGING at 1000 Hz 
                    if logging_enabled_flag["val"] == 1:
                        rows = [s.tolist() for s in data]
                        try:
                            csv_queue.put_nowait({
                                'time_s': batch_start_time,
                                'rows': rows,
                                'kind': 'LC'
                            })
                        except Full:
                            pass

                    # Downsample for display only (100 Hz)
                    display_data = data[::10]
                    try:
                        lc_queue.put_nowait({
                            'time': batch_start_time,
                            'data': display_data.copy(),
                            'sensor_type': 'LC'
                        })
                    except Full:
                        pass

                except Exception as e:
                    print(f"LC callback error: {e}")
                    return -1
                return 0

            task.register_every_n_samples_acquired_into_buffer_event(update_interval, callback)
            task.start()
            print("LC thread running (1000 Hz logging, 100 Hz display)")

            while not stop_event.is_set():
                time.sleep(0.1)

    except Exception as e:
        print(f"LC reader error: {e}")
    finally:
        print("LC thread stopping")




def module_9485_worker(stop_event, continuity_queue):
    

    # ------------------------
    # Constants / Mapping
    # ------------------------
    loop = sy.Loop(sy.Rate.HZ * 10)  # 10 Hz
    NUM_RELAY_CH = 16

    # Vent (old Module A)
    VENT_CH = 3
    CLOSE_SEC = 120
    OPEN_SEC = 3

    # Hotfire mapping (old Module B)
    MFV_CH = 8       # old B CH0
    MOV_CH = 9       # old B CH1
    PURGE_CH = 14    # old B CH2
    IGNITOR_CH = 15  # old B CH7

    HOTFIRE_LEAD_TIME = 0.0625
    HOTFIRE_BURN_TIME = 10.0
    HOTFIRE_PURGE_TIME = 5.0
    COUNTDOWN_STEP = 0.1

    # ------------------------
    # Synnax Channels (Unified 0..15)
    # ------------------------
    NI9485_TIME = client.channels.create(
        name="NI9485Time",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )

    shiftButtonChannel = client.channels.create(
        name="ShiftButtonState",
        data_type=sy.DataType.UINT8,
        index=NI9485_TIME.key,
        retrieve_if_name_exists=True,
    )

    # Per-relay CMD/CMD_TIME and STATE for CH0..15
    cmd_time_ch, cmd_ch, state_ch = [], [], []
    for i in range(NUM_RELAY_CH):
        t = client.channels.create(
            name=f"NI_9485_CH{i}_CMD_TIME",
            is_index=True,
            data_type=sy.DataType.TIMESTAMP,
            retrieve_if_name_exists=True,
        )
        c = client.channels.create(
            name=f"NI_9485_CH{i}_CMD",
            data_type=sy.DataType.UINT8,
            index=t.key,
            retrieve_if_name_exists=True,
        )
        s = client.channels.create(
            name=f"NI_9485_CH{i}_STATE",
            data_type=sy.DataType.UINT8,
            index=NI9485_TIME.key,
            retrieve_if_name_exists=True,
        )
        cmd_time_ch.append(t); cmd_ch.append(c); state_ch.append(s)

    # Main lockout (single)
    main_lockout_state_time = client.channels.create(
        name="NI9485_MAIN_LOCKOUT_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    main_lockout_time = client.channels.create(
        name="NI9485_MAIN_LOCKOUT_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    main_lockout_cmd = client.channels.create(
        name="NI9485_MAIN_LOCKOUT_CMD",
        data_type=sy.DataType.UINT8,
        index=main_lockout_time.key,
        retrieve_if_name_exists=True
    )
    main_lockout_state = client.channels.create(
        name="NI9485_MAIN_LOCKOUT_STATE",
        data_type=sy.DataType.UINT8,
        index=main_lockout_state_time.key,
        retrieve_if_name_exists=True
    )

    # Vent enable/run + cycler (same names you already used)
    ox_enable_state_time = client.channels.create(
        name="OX_VENT_ENABLE_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    ox_enable_state = client.channels.create(
        name="OX_VENT_ENABLE_STATE",
        data_type=sy.DataType.UINT8,
        index=ox_enable_state_time.key,
        retrieve_if_name_exists=True,
    )
    ox_enable_cmd_time = client.channels.create(
        name="OX_VENT_ENABLE_CMD_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    ox_enable_cmd = client.channels.create(
        name="OX_VENT_ENABLE_CMD",
        data_type=sy.DataType.UINT8,
        index=ox_enable_cmd_time.key,
        retrieve_if_name_exists=True,
    )

    ox_run_state_time = client.channels.create(
        name="OX_VENT_RUN_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    ox_run_state = client.channels.create(
        name="OX_VENT_RUN_STATE",
        data_type=sy.DataType.UINT8,
        index=ox_run_state_time.key,
        retrieve_if_name_exists=True,
    )
    ox_run_cmd_time = client.channels.create(
        name="OX_VENT_RUN_CMD_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    ox_run_cmd = client.channels.create(
        name="OX_VENT_RUN_CMD",
        data_type=sy.DataType.UINT8,
        index=ox_run_cmd_time.key,
        retrieve_if_name_exists=True,
    )

    ox_cycler_state_time = client.channels.create(
        name="OX_VENT_CYCLER_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    ox_cycler_state = client.channels.create(
        name="OX_VENT_CYCLER_STATE",
        data_type=sy.DataType.UINT8,
        index=ox_cycler_state_time.key,
        retrieve_if_name_exists=True,
    )

    # MPVA lockout (old Module B)
    mpva_lockout_state_time = client.channels.create(
        name="NI9485_MPVA_LOCKOUT_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    mpva_lockout_time = client.channels.create(
        name="NI9485_MPVA_LOCKOUT_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    mpva_lockout_cmd = client.channels.create(
        name="NI9485_MPVA_LOCKOUT_CMD",
        data_type=sy.DataType.UINT8,
        index=mpva_lockout_time.key,
        retrieve_if_name_exists=True
    )
    mpva_lockout_state = client.channels.create(
        name="NI9485_MPVA_LOCKOUT_STATE",
        data_type=sy.DataType.UINT8,
        index=mpva_lockout_state_time.key,
        retrieve_if_name_exists=True
    )

    # Hotfire enable (old Module B)
    auto_enable_state_time = client.channels.create(
        name="NI9485_HOTFIRE_ENABLE_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    auto_enable_time = client.channels.create(
        name="NI9485_HOTFIRE_ENABLE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    auto_enable_cmd = client.channels.create(
        name="NI9485_HOTFIRE_ENABLE_CMD",
        data_type=sy.DataType.UINT8,
        index=auto_enable_time.key,
        retrieve_if_name_exists=True
    )
    auto_enable_state = client.channels.create(
        name="NI9485_HOTFIRE_ENABLE_STATE",
        data_type=sy.DataType.UINT8,
        index=auto_enable_state_time.key,
        retrieve_if_name_exists=True
    )

    # MPVA Actuation (old Module B)
    mpva_actuation_state_time = client.channels.create(
        name="NI9485_MPVA_ACTUATION_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    mpva_actuation_state = client.channels.create(
        name="NI9485_MPVA_ACTUATION_STATE",
        data_type=sy.DataType.UINT8,
        index=mpva_actuation_state_time.key,
        retrieve_if_name_exists=True,
    )
    mpva_actuation_cmd_time = client.channels.create(
        name="NI9485_MPVA_ACTUATION_CMD_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True,
    )
    mpva_actuation_cmd = client.channels.create(
        name="NI9485_MPVA_ACTUATION_CMD",
        data_type=sy.DataType.UINT8,
        index=mpva_actuation_cmd_time.key,
        retrieve_if_name_exists=True,
    )

    # Hotfire run (old Module B)
    cycle_lockout_state_time = client.channels.create(
        name="NI9485_HOTFIRE_RUN_STATE_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    cycle_lockout_time = client.channels.create(
        name="NI9485_HOTFIRE_RUN_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    cycle_lockout_cmd = client.channels.create(
        name="NI9485_HOTFIRE_RUN_CMD",
        data_type=sy.DataType.UINT8,
        index=cycle_lockout_time.key,
        retrieve_if_name_exists=True
    )
    cycle_lockout_state = client.channels.create(
        name="NI9485_HOTFIRE_RUN_STATE",
        data_type=sy.DataType.UINT8,
        index=cycle_lockout_state_time.key,
        retrieve_if_name_exists=True
    )

    # Countdown (old Module B)
    countdown_time_channel = client.channels.create(
        name="NI9485_HOTFIRE_COUNTDOWN_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    )
    countdown_channel = client.channels.create(
        name="NI9485_HOTFIRE_COUNTDOWN",
        data_type=sy.DataType.FLOAT32,
        index=countdown_time_channel.key,
        retrieve_if_name_exists=True
    )

    # ------------------------
    # Streamer + Writer lists
    # ------------------------
    cmdNameList = [f"NI_9485_CH{i}_CMD" for i in range(NUM_RELAY_CH)]

    stream_names = cmdNameList + [
        "NI9485_MAIN_LOCKOUT_CMD",
        "OX_VENT_ENABLE_CMD",
        "OX_VENT_RUN_CMD",
        "NI9485_MPVA_LOCKOUT_CMD",
        "NI9485_HOTFIRE_ENABLE_CMD",
        "NI9485_HOTFIRE_RUN_CMD",
        "NI9485_MPVA_ACTUATION_CMD",
    ]

    writeChannels = [NI9485_TIME] + state_ch + [
        main_lockout_state_time, main_lockout_state,
        mpva_lockout_state_time, mpva_lockout_state,
        auto_enable_state_time, auto_enable_state,
        cycle_lockout_state_time, cycle_lockout_state,
        countdown_time_channel, countdown_channel,
        shiftButtonChannel,
        mpva_actuation_state_time, mpva_actuation_state,
        ox_enable_state_time, ox_enable_state,
        ox_run_state_time, ox_run_state,
        ox_cycler_state_time, ox_cycler_state,
    ]

    # ------------------------
    # Utilities (keep your writer bump helper)
    # ------------------------
    def open_writer_no_overlap(channels, start_hint=None, max_bumps=20, bump_seconds=30):
        start = start_hint or sy.TimeStamp.now()
        bumps = 0
        while True:
            try:
                return client.open_writer(start=start, channels=channels, enable_auto_commit=True)
            except Exception as e:
                msg = str(e)
                if "overlaps with existing data" in msg or "cannot open writer" in msg:
                    bumps += 1
                    if bumps > max_bumps:
                        raise
                    start = start + sy.TimeSpan.SECOND * bump_seconds
                    print(f"[Writer] bumped start (#{bumps}) → {start}")
                    time.sleep(0.05)
                else:
                    raise

    def write_states(writer, st):
        now = sy.TimeStamp.now()
        st["NI9485Time"] = now
        st["NI9485_MAIN_LOCKOUT_STATE_TIME"] = now
        st["NI9485_MPVA_LOCKOUT_STATE_TIME"] = now
        st["NI9485_HOTFIRE_ENABLE_STATE_TIME"] = now
        st["NI9485_HOTFIRE_RUN_STATE_TIME"] = now
        st["NI9485_MPVA_ACTUATION_STATE_TIME"] = now
        st["NI9485_HOTFIRE_COUNTDOWN_TIME"] = now
        st["OX_VENT_ENABLE_STATE_TIME"] = now
        st["OX_VENT_RUN_STATE_TIME"] = now
        st["OX_VENT_CYCLER_STATE_TIME"] = now
        writer.write(st)

    # ------------------------
    # Vent cycler (UNCHANGED logic, just uses CH3 in 0..15)
    # ------------------------
    def ox_vent_cycler(stop_evt, writer, st, ni_task, writeStates, ch_index=VENT_CH, close_s=CLOSE_SEC, open_s=OPEN_SEC):
        print("[OX] cycler: START")
        try:
            while not stop_evt.is_set():
                writeStates[ch_index] = False
                st[f"NI_9485_CH{ch_index}_STATE"] = 0
                ni_task.write(writeStates); write_states(writer, st)
                t = 0.0
                while t < close_s and not stop_evt.is_set():
                    time.sleep(0.1); t += 0.1

                writeStates[ch_index] = True
                st[f"NI_9485_CH{ch_index}_STATE"] = 1
                ni_task.write(writeStates); write_states(writer, st)
                t = 0.0
                while t < open_s and not stop_evt.is_set():
                    time.sleep(0.1); t += 0.1
        finally:
            writeStates[ch_index] = False
            st[f"NI_9485_CH{ch_index}_STATE"] = 0
            ni_task.write(writeStates); write_states(writer, st)
            print(f"[OX] cycler: STOP, CH{ch_index} closed")

    # ------------------------
    # Hotfire countdown thread (UNCHANGED)
    # ------------------------
    countdown_stop_event = threading.Event()
    countdown_thread = None

    def countdown_timer():
        countdown = 10.0
        while countdown >= 0 and not countdown_stop_event.is_set():
            try:
                current_time = sy.TimeStamp.now()
                st["NI9485_HOTFIRE_COUNTDOWN_TIME"] = current_time
                st["NI9485_HOTFIRE_COUNTDOWN"] = float(countdown)
                writer.write(st)
                countdown -= COUNTDOWN_STEP
                time.sleep(COUNTDOWN_STEP)
            except Exception as e:
                print(f"Countdown write error: {e}")
                break

        if not countdown_stop_event.is_set():
            try:
                current_time = sy.TimeStamp.now()
                st["NI9485_HOTFIRE_COUNTDOWN_TIME"] = current_time
                st["NI9485_HOTFIRE_COUNTDOWN"] = 0.0
                writer.write(st)
            except Exception as e:
                print(f"Final countdown write error: {e}")

    # ------------------------
    # Abort hotfire (same behavior, remapped channels)
    # ------------------------
    def abort_sequence(message):
        nonlocal auto_enable_enabled, latest_auto_enable, cycle_lockout_enabled, latest_cycle_lockout
        print(f"Aborting auto hotfire - {message}")

        for ch in [MFV_CH, MOV_CH, PURGE_CH, IGNITOR_CH]:
            writeStates[ch] = False
            st[f"NI_9485_CH{ch}_STATE"] = 0

        current_time = sy.TimeStamp.now()
        st["NI9485Time"] = current_time
        ni_task.write(writeStates)
        writer.write(st)

        st["NI9485_HOTFIRE_RUN_STATE"] = 0
        st["NI9485_HOTFIRE_RUN_STATE_TIME"] = current_time
        st["NI9485_HOTFIRE_ENABLE_STATE"] = 0
        st["NI9485_HOTFIRE_ENABLE_STATE_TIME"] = current_time

        auto_enable_enabled = False
        latest_auto_enable = 0
        cycle_lockout_enabled = False
        latest_cycle_lockout = 0

        print("Auto enable and cycle lockout reset to DISABLED after abort")
        countdown_stop_event.set()
        try:
            countdown_thread.join(timeout=2)
        except Exception:
            pass
        writer.write(st)

    # ------------------------
    # State vars (union of A + B, but single set)
    # ------------------------
    writeStates = [False] * NUM_RELAY_CH

    main_lockout = False

    # Vent vars
    ox_enable = 0
    ox_run = 0
    cycler_active = 0
    cycler_stop = None
    cycler_thr = None

    # Hotfire vars (as in Module B)
    main_lockout_enabled = False
    mpva_lockout_enabled = False
    auto_enable_enabled = False
    cycle_lockout_enabled = False

    latest_main_lockout = 0
    latest_mpva_lockout = 0
    latest_auto_enable = 0
    latest_cycle_lockout = 0
    latest_mpva_actuation = 0

    latest_relay_cmds = [0] * NUM_RELAY_CH

    # ------------------------
    # Initial state dict
    # ------------------------
    init_time = sy.TimeStamp.now()
    st = {"NI9485Time": init_time, "ShiftButtonState": 0}

    for i in range(NUM_RELAY_CH):
        st[f"NI_9485_CH{i}_STATE"] = 0

    st.update({
        "NI9485_MAIN_LOCKOUT_STATE_TIME": init_time,
        "NI9485_MAIN_LOCKOUT_STATE": 0,

        "NI9485_MPVA_LOCKOUT_STATE_TIME": init_time,
        "NI9485_MPVA_LOCKOUT_STATE": 0,

        "NI9485_HOTFIRE_ENABLE_STATE_TIME": init_time,
        "NI9485_HOTFIRE_ENABLE_STATE": 0,

        "NI9485_HOTFIRE_RUN_STATE_TIME": init_time,
        "NI9485_HOTFIRE_RUN_STATE": 0,

        "NI9485_MPVA_ACTUATION_STATE_TIME": init_time,
        "NI9485_MPVA_ACTUATION_STATE": 0,

        "NI9485_HOTFIRE_COUNTDOWN_TIME": init_time,
        "NI9485_HOTFIRE_COUNTDOWN": 0.0,

        "OX_VENT_ENABLE_STATE_TIME": init_time,
        "OX_VENT_ENABLE_STATE": 0,
        "OX_VENT_RUN_STATE_TIME": init_time,
        "OX_VENT_RUN_STATE": 0,
        "OX_VENT_CYCLER_STATE_TIME": init_time,
        "OX_VENT_CYCLER_STATE": 0,
    })

    # ------------------------
    # Run
    # ------------------------
    try:
        with nidaqmx.Task() as ni_task, \
             client.open_streamer(stream_names) as streamer, \
             open_writer_no_overlap(writeChannels, start_hint=sy.TimeStamp.now()) as writer:

            # One NI task controls BOTH relay modules
            ni_task.do_channels.add_do_chan(
                "cDAQ1Mod5/port0/line0:7",
                line_grouping=LineGrouping.CHAN_PER_LINE,
            )
            ni_task.do_channels.add_do_chan(
                "cDAQ1Mod6/port0/line0:7",
                line_grouping=LineGrouping.CHAN_PER_LINE,
            )

            ni_task.start()
            ni_task.write(writeStates)
            write_states(writer, st)

            print("Unified 9485 worker up. CH0-7=Mod5, CH8-15=Mod6. Vent on CH3. Hotfire uses CH8/9/10/15.")
            print("Shift required for lockouts, enables, MPVA actuation, hotfire trigger, and manual relay commands.")

            while not stop_event.is_set() and loop.wait():
                shift_pressed = keyboard.is_pressed('shift')
                st["ShiftButtonState"] = 1 if shift_pressed else 0

                frame = streamer.read(timeout=0.5)
                changed = False
                loop_time = sy.TimeStamp.now()

                if frame is not None:
                    # --------------------------
                    # MAIN LOCKOUT (global)
                    # --------------------------
                    v = frame.get("NI9485_MAIN_LOCKOUT_CMD")
                    if v is not None and len(v) > 0:
                        latest = int(v[-1])
                        if latest != int(main_lockout_enabled):
                            if shift_pressed:
                                main_lockout_enabled = bool(latest)
                                main_lockout = main_lockout_enabled
                                st["NI9485_MAIN_LOCKOUT_STATE"] = latest
                                st["NI9485_MAIN_LOCKOUT_STATE_TIME"] = loop_time
                                changed = True

                                if not main_lockout_enabled:
                                    # Force ALL relays off (matches both modules' intent)
                                    for i in range(NUM_RELAY_CH):
                                        writeStates[i] = False
                                        st[f"NI_9485_CH{i}_STATE"] = 0

                                    # Stop vent cycler
                                    if cycler_active:
                                        cycler_stop.set()
                                        cycler_thr.join(timeout=2)
                                        cycler_active = 0
                                        st["OX_VENT_CYCLER_STATE"] = 0

                                    # Reset B-side latches
                                    mpva_lockout_enabled = False
                                    auto_enable_enabled = False
                                    cycle_lockout_enabled = False
                                    latest_mpva_lockout = 0
                                    latest_auto_enable = 0
                                    latest_cycle_lockout = 0

                                    st["NI9485_MPVA_LOCKOUT_STATE"] = 0
                                    st["NI9485_HOTFIRE_ENABLE_STATE"] = 0
                                    st["NI9485_HOTFIRE_RUN_STATE"] = 0
                                    st["NI9485_MPVA_ACTUATION_STATE"] = 0
                                    st["NI9485_HOTFIRE_COUNTDOWN"] = 0.0

                                    # Reset vent enables
                                    ox_enable = 0
                                    ox_run = 0
                                    st["OX_VENT_ENABLE_STATE"] = 0
                                    st["OX_VENT_RUN_STATE"] = 0

                                    print("[MainLockout] OFF → all relays OFF, cycler OFF, hotfire latches reset")
                            else:
                                print("Shift not pressed, ignoring MAIN LOCKOUT command")
                                # Keep A-style write-then-revert behavior so GUI doesn't "stick"
                                writer.write({"NI9485_MAIN_LOCKOUT_STATE": latest, "NI9485_MAIN_LOCKOUT_STATE_TIME": sy.TimeStamp.now()})
                                writer.write({"NI9485_MAIN_LOCKOUT_STATE": int(main_lockout_enabled), "NI9485_MAIN_LOCKOUT_STATE_TIME": sy.TimeStamp.now()})

                    # --------------------------
                    # VENT enable/run (same as Module A)
                    # --------------------------
                    v = frame.get("OX_VENT_ENABLE_CMD")
                    if v is not None and len(v) > 0:
                        new_en = int(v[-1])
                        if new_en != ox_enable:
                            if shift_pressed:
                                ox_enable = new_en
                                st["OX_VENT_ENABLE_STATE"] = new_en
                                changed = True
                            else:
                                print("Shift not pressed, ignoring OX_VENT_ENABLE")
                                writer.write({"OX_VENT_ENABLE_STATE": new_en, "OX_VENT_ENABLE_STATE_TIME": sy.TimeStamp.now()})
                                writer.write({"OX_VENT_ENABLE_STATE": ox_enable, "OX_VENT_ENABLE_STATE_TIME": sy.TimeStamp.now()})

                    v = frame.get("OX_VENT_RUN_CMD")
                    if v is not None and len(v) > 0:
                        new_run = int(v[-1])
                        if new_run != ox_run:
                            if shift_pressed:
                                ox_run = new_run
                                st["OX_VENT_RUN_STATE"] = new_run
                                changed = True
                            else:
                                print("Shift not pressed, ignoring OX_VENT_RUN")
                                writer.write({"OX_VENT_RUN_STATE": new_run, "OX_VENT_RUN_STATE_TIME": sy.TimeStamp.now()})
                                writer.write({"OX_VENT_RUN_STATE": ox_run, "OX_VENT_RUN_STATE_TIME": sy.TimeStamp.now()})

                    # --------------------------
                    # MPVA LOCKOUT (same as Module B)
                    # --------------------------
                    mpva_lockout_data = frame.get("NI9485_MPVA_LOCKOUT_CMD")
                    if mpva_lockout_data is not None and len(mpva_lockout_data) > 0:
                        latest = mpva_lockout_data[-1]
                        if latest != latest_mpva_lockout:
                            if shift_pressed:
                                latest_mpva_lockout = latest
                                mpva_lockout_enabled = bool(latest)
                                st["NI9485_MPVA_LOCKOUT_STATE"] = int(latest)
                                st["NI9485_MPVA_LOCKOUT_STATE_TIME"] = loop_time
                                changed = True
                                print(f"MPVA lockout state changed: {'ENABLED' if mpva_lockout_enabled else 'DISABLED'}")
                                if not mpva_lockout_enabled:
                                    # Force MFV/MOV OFF
                                    writeStates[MFV_CH] = False
                                    writeStates[MOV_CH] = False
                                    st[f"NI_9485_CH{MFV_CH}_STATE"] = 0
                                    st[f"NI_9485_CH{MOV_CH}_STATE"] = 0
                                    latest_relay_cmds[MFV_CH] = 0
                                    latest_relay_cmds[MOV_CH] = 0
                                    print("MPVA lockout disabled - MFV/MOV turned OFF")
                                    ni_task.write(writeStates)
                                    writer.write(st)
                                else:
                                    latest_relay_cmds[MFV_CH] = 0
                                    latest_relay_cmds[MOV_CH] = 0
                                    print("MFV/MOV reset command tracking")
                            else:
                                print("Shift not pressed, ignoring MPVA LOCKOUT command")

                    # --------------------------
                    # HOTFIRE ENABLE (same as Module B)
                    # --------------------------
                    auto_enable_data = frame.get("NI9485_HOTFIRE_ENABLE_CMD")
                    if auto_enable_data is not None and len(auto_enable_data) > 0:
                        latest = auto_enable_data[-1]
                        if latest != latest_auto_enable:
                            if shift_pressed:
                                latest_auto_enable = latest
                                auto_enable_enabled = bool(latest)
                                st["NI9485_HOTFIRE_ENABLE_STATE"] = int(latest)
                                st["NI9485_HOTFIRE_ENABLE_STATE_TIME"] = loop_time
                                changed = True
                                print(f"Auto enable state changed: {'ENABLED' if auto_enable_enabled else 'DISABLED'}")
                            else:
                                print("Shift not pressed, ignoring AUTO ENABLE command")

                    # --------------------------
                    # MPVA ACTUATION (same as Module B logic, remapped channels)
                    # --------------------------
                    mpva_actuation_data = frame.get("NI9485_MPVA_ACTUATION_CMD")
                    if mpva_actuation_data is not None and len(mpva_actuation_data) > 0:
                        latest = mpva_actuation_data[-1]
                        if latest != latest_mpva_actuation:
                            if shift_pressed:
                                if main_lockout_enabled and mpva_lockout_enabled:
                                    latest_mpva_actuation = latest
                                    st["NI9485_MPVA_ACTUATION_STATE"] = int(latest)
                                    st["NI9485_MPVA_ACTUATION_STATE_TIME"] = loop_time

                                    if latest:  # open sequence
                                        writeStates[MFV_CH] = True
                                        st[f"NI_9485_CH{MFV_CH}_STATE"] = 1
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print("MPVA ENABLE ON → MFV set to 1")

                                        time.sleep(HOTFIRE_LEAD_TIME)

                                        writeStates[MOV_CH] = True
                                        st[f"NI_9485_CH{MOV_CH}_STATE"] = 1
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print("MPVA ENABLE ON → MOV set to 1")
                                    else:       # close sequence
                                        writeStates[MOV_CH] = False
                                        st[f"NI_9485_CH{MOV_CH}_STATE"] = 0
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        time.sleep(HOTFIRE_LEAD_TIME)

                                        writeStates[MFV_CH] = False
                                        st[f"NI_9485_CH{MFV_CH}_STATE"] = 0
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print("MPVA ENABLE OFF → MFV/MOV set to 0")
                                else:
                                    print("MPVA ENABLE ignored - require MAIN LOCKOUT and MPVA LOCKOUT enabled")
                            else:
                                print("Shift not pressed, ignoring MPVA ENABLE command")

                    # --------------------------
                    # HOTFIRE RUN (THIS IS YOUR MODULE B SEQUENCE, UNCHANGED, REMAPPED CHANNELS)
                    # --------------------------
                    cycle_lockout_data = frame.get("NI9485_HOTFIRE_RUN_CMD")
                    if cycle_lockout_data is not None and len(cycle_lockout_data) > 0:
                        latest = cycle_lockout_data[-1]
                        if latest != latest_cycle_lockout:
                            if shift_pressed:
                                latest_cycle_lockout = latest
                                cycle_lockout_enabled = bool(latest)
                                st["NI9485_HOTFIRE_RUN_STATE"] = int(latest)
                                st["NI9485_HOTFIRE_RUN_STATE_TIME"] = loop_time
                                changed = True

                                if latest == 1 and cycle_lockout_enabled:
                                    if main_lockout_enabled and mpva_lockout_enabled and auto_enable_enabled:
                                        print("\n=== Auto hotfire trigger detected! Starting sequence... ===")
                                        countdown_stop_event.clear()
                                        countdown_thread = threading.Thread(target=countdown_timer, name="HOTFIRE_COUNTDOWN", daemon=True)
                                        countdown_thread.start()

                                        with continuity_queue.mutex:
                                            continuity_queue.queue.clear()

                                        print("Waiting 5.5 seconds for ignitor continuity check...")
                                        time.sleep(5.5)

                                        recent_pt31 = []
                                        while not continuity_queue.empty():
                                            recent_pt31.append(continuity_queue.get())

                                        if recent_pt31:
                                            avg_pt31 = sum(recent_pt31) / len(recent_pt31)
                                            recent_tail = recent_pt31[-10:]
                                            has_low_sample = any(v <= 1 for v in recent_tail)
                                            print(f"PT31 average during continuity check: {avg_pt31:.2f} V")
                                            if avg_pt31 <= 1:
                                                abort_sequence("No continuity detected in first 5 seconds")
                                                continue
                                        else:
                                            abort_sequence("No PT31 data received during continuity check")
                                            continue

                                        # Ignitor ON (remapped)
                                        current_time = sy.TimeStamp.now()
                                        writeStates[IGNITOR_CH] = True
                                        st[f"NI_9485_CH{IGNITOR_CH}_STATE"] = 1
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"Ignitor (CH{IGNITOR_CH}) turned ON")
                                        print(f"Relay states: {writeStates}")

                                        with continuity_queue.mutex:
                                            continuity_queue.queue.clear()

                                        # 5 sec discontinuity check
                                        time.sleep(5.0)
                                        recent_pt31 = []
                                        while not continuity_queue.empty():
                                            recent_pt31.append(continuity_queue.get())

                                        if recent_pt31:
                                            has_loss = any(v < 1 for v in recent_pt31)
                                            print(f"PT31 min during ignitor fire check: {min(recent_pt31):.2f} V")
                                            if not has_loss:
                                                print("No continuity loss in second 5s window — extending for 10s")
                                                extended_start = time.time()
                                                loss_detected = False

                                                while time.time() - extended_start < 10.0:
                                                    if not continuity_queue.empty():
                                                        pt_val = continuity_queue.get()
                                                        if pt_val < 1:
                                                            print(f"Continuity lost at {time.time()-extended_start:.2f}s into extended window")
                                                            loss_detected = True
                                                            time.sleep(1.0)
                                                            break
                                                    time.sleep(0.1)

                                                if not loss_detected:
                                                    abort_sequence("Continuity not lost in extended 10s window")
                                                    continue
                                        else:
                                            # If no data, treat as no loss and extend (matches spirit of original)
                                            print("No PT31 samples in 5s window — extending for 10s")
                                            extended_start = time.time()
                                            loss_detected = False
                                            while time.time() - extended_start < 10.0:
                                                if not continuity_queue.empty():
                                                    pt_val = continuity_queue.get()
                                                    if pt_val < 1:
                                                        loss_detected = True
                                                        time.sleep(1.0)
                                                        break
                                                time.sleep(0.1)
                                            if not loss_detected:
                                                abort_sequence("Continuity not lost in extended 10s window")
                                                continue

                                        # OPEN MFV (Fuel) (remapped)
                                        current_time = sy.TimeStamp.now()
                                        writeStates[IGNITOR_CH] = False
                                        writeStates[MFV_CH] = True
                                        st[f"NI_9485_CH{IGNITOR_CH}_STATE"] = 0
                                        st[f"NI_9485_CH{MFV_CH}_STATE"] = 1
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"Ignitor OFF, Fuel (CH{MFV_CH}) ON")
                                        print(f"Relay states: {writeStates}")
                                        time.sleep(HOTFIRE_LEAD_TIME)

                                        # OPEN MOV (Ox) (remapped)
                                        current_time = sy.TimeStamp.now()
                                        writeStates[MOV_CH] = True
                                        st[f"NI_9485_CH{MOV_CH}_STATE"] = 1
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"Oxidizer (CH{MOV_CH}) ON")
                                        print(f"Relay states: {writeStates}")
                                        time.sleep(HOTFIRE_BURN_TIME)

                                        # CLOSE MOV then MFV (this matches your later B version)
                                        current_time = sy.TimeStamp.now()
                                        writeStates[MOV_CH] = False
                                        st[f"NI_9485_CH{MOV_CH}_STATE"] = 0
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"Oxidizer (CH{MOV_CH}) OFF")
                                        time.sleep(HOTFIRE_LEAD_TIME)

                                        writeStates[MFV_CH] = False
                                        st[f"NI_9485_CH{MFV_CH}_STATE"] = 0
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"Fuel (CH{MFV_CH}) OFF")
                                        print(f"Relay states: {writeStates}")

                                        # PURGE
                                        current_time = sy.TimeStamp.now()
                                        writeStates[PURGE_CH] = True
                                        st[f"NI_9485_CH{PURGE_CH}_STATE"] = 1
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"N2 Purge (CH{PURGE_CH}) ON")
                                        print(f"Relay states: {writeStates}")
                                        time.sleep(HOTFIRE_PURGE_TIME)

                                        current_time = sy.TimeStamp.now()
                                        writeStates[PURGE_CH] = False
                                        st[f"NI_9485_CH{PURGE_CH}_STATE"] = 0
                                        st["NI9485Time"] = current_time
                                        ni_task.write(writeStates)
                                        writer.write(st)
                                        print(f"N2 Purge (CH{PURGE_CH}) OFF")
                                        print(f"Relay states: {writeStates}")

                                        print("\n=== Auto hotfire sequence complete! ===")
                                        current_time = sy.TimeStamp.now()
                                        st["NI9485_HOTFIRE_RUN_STATE"] = 0
                                        st["NI9485_HOTFIRE_RUN_STATE_TIME"] = current_time
                                        st["NI9485_HOTFIRE_ENABLE_STATE"] = 0
                                        st["NI9485_HOTFIRE_ENABLE_STATE_TIME"] = current_time
                                        auto_enable_enabled = False
                                        latest_auto_enable = 0
                                        cycle_lockout_enabled = False
                                        latest_cycle_lockout = 0
                                        print("Auto enable and cycle lockout reset to DISABLED after sequence completion")
                                        countdown_stop_event.set()
                                        countdown_thread.join(timeout=2)
                                        writer.write(st)
                                    else:
                                        print("Auto hotfire request ignored - not all lockouts and auto enable unlocked")
                            else:
                                print("Shift not pressed, ignoring HOTFIRE RUN command")

                    # --------------------------
                    # MANUAL RELAY COMMANDS (now for CH0..15)
                    # - Same gating: require main_lockout and shift
                    # - Keep A behavior: if vent cycler active, ignore manual vent
                    # - Keep B behavior: if MPVA lockout disabled, ignore MFV/MOV manual
                    # --------------------------
                    if main_lockout_enabled:
                        for i in range(NUM_RELAY_CH):
                            v = frame.get(f"NI_9485_CH{i}_CMD")
                            if v is None or len(v) == 0:
                                continue
                            desired = int(v[-1])

                            if i == VENT_CH and cycler_active:
                                continue

                            if desired != int(writeStates[i]):
                                if shift_pressed:
                                    # MPVA lockout rule (same as B)
                                    if (i == MFV_CH or i == MOV_CH) and not mpva_lockout_enabled:
                                        print(f"Manual CH{i} ignored - MPVA lockout disabled")
                                        continue

                                    writeStates[i] = bool(desired)
                                    st[f"NI_9485_CH{i}_STATE"] = desired
                                    changed = True
                                else:
                                    print(f"Shift not pressed, ignoring Manual CH{i} command")
                                    # A-style revert write (so GUI doesn't latch wrong)
                                    temp_frame = {"NI9485Time": sy.TimeStamp.now(), "ShiftButtonState": st["ShiftButtonState"]}
                                    for j in range(NUM_RELAY_CH):
                                        temp_frame[f"NI_9485_CH{j}_STATE"] = desired if j == i else int(writeStates[j])
                                    writer.write(temp_frame)

                                    temp_frame2 = {"NI9485Time": sy.TimeStamp.now(), "ShiftButtonState": st["ShiftButtonState"]}
                                    for j in range(NUM_RELAY_CH):
                                        temp_frame2[f"NI_9485_CH{j}_STATE"] = int(writeStates[j])
                                    writer.write(temp_frame2)
                    else:
                        forced = False
                        for i in range(NUM_RELAY_CH):
                            if writeStates[i]:
                                writeStates[i] = False
                                st[f"NI_9485_CH{i}_STATE"] = 0
                                forced = True
                        if cycler_active:
                            cycler_stop.set(); cycler_thr.join(timeout=2)
                            cycler_active = 0
                            st["OX_VENT_CYCLER_STATE"] = 0
                            forced = True
                        if forced:
                            changed = True
                            print("[MainLockout] OFF → relays forced OFF, cycler stopped")

                # --------------------------
                # Vent cycler start/stop (UNCHANGED)
                # --------------------------
                should_run = (ox_enable == 1 and ox_run == 1 and main_lockout_enabled)
                if should_run and not cycler_active:
                    cycler_stop = threading.Event()
                    cycler_thr = threading.Thread(
                        target=ox_vent_cycler,
                        args=(cycler_stop, writer, st, ni_task, writeStates, VENT_CH, CLOSE_SEC, OPEN_SEC),
                        name="OX_VENT_CYCLER",
                        daemon=True,
                    )
                    cycler_thr.start()
                    cycler_active = 1
                    st["OX_VENT_CYCLER_STATE"] = 1
                    changed = True

                if (not should_run) and cycler_active:
                    cycler_stop.set()
                    cycler_thr.join(timeout=2)
                    cycler_active = 0
                    st["OX_VENT_CYCLER_STATE"] = 0
                    writeStates[VENT_CH] = False
                    st[f"NI_9485_CH{VENT_CH}_STATE"] = 0
                    changed = True

                # --------------------------
                # Final write (same pattern)
                # --------------------------
                if changed:
                    st["NI9485Time"] = sy.TimeStamp.now()
                    if shift_pressed or not main_lockout_enabled:
                        ni_task.write(writeStates)
                    write_states(writer, st)
                else:
                    write_states(writer, st)

    except Exception as e:
        print(f"Unified 9485 worker error: {e}")

    finally:
        # Safety: force all relays OFF on exit
        try:
            with nidaqmx.Task() as ni_task:
                ni_task.do_channels.add_do_chan(
                    "cDAQ1Mod5/port0/line0:7",
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                ni_task.do_channels.add_do_chan(
                    "cDAQ1Mod6/port0/line0:7",
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                ni_task.start()
                ni_task.write([False] * NUM_RELAY_CH)
                print("Unified 9485 worker: All relays turned OFF on exit")
        except Exception as e:
            print(f"Unified 9485 worker: NI cleanup error: {e}")
        print("Unified 9485 worker stopped")



# Main

if __name__ == "__main__":
    stop_event = threading.Event()
    
    # Create queues
    pt_queue = Queue(maxsize=1000)
    tc_queue = Queue(maxsize=200)
    lc_queue = Queue(maxsize=500)
    csv_queue = Queue(maxsize=5000)
    continuity_queue = Queue(maxsize=500)
    
    # Create threads
    thread_configs = [
        ("Logging Controller", logging_controller, (stop_event, csv_queue)),
        ("PT Reader", pt_reader, (stop_event, pt_queue, continuity_queue, csv_queue)),
        ("TC Reader", tc_reader, (stop_event, tc_queue, csv_queue)),
        ("LC Reader", lc_reader, (stop_event, lc_queue, csv_queue)),
        ("CSV Writer", csv_writer, (stop_event, csv_queue)),
        ("PT Synnax", synnax_pt_writer_robust, (stop_event, pt_queue, client, pt_time_channel, pt_raw_channels, pt_scaled_channels, PT_SCALING)),
        ("TC Synnax", synnax_tc_writer_robust, (stop_event, tc_queue, client, tc_time_channel, tc_channels)),
        ("LC Synnax", synnax_lc_writer_robust, (stop_event, lc_queue, client, lc_time_channel, lc_channels)),
        ("Health Monitor", telemetry_health_monitor, (stop_event, pt_queue, tc_queue, lc_queue, csv_queue)),
        ("Valve Module 9485", module_9485_worker, (stop_event, continuity_queue)),
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
        print("CSV files rotate on rising edge of LOG_ENABLE_CMD")
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