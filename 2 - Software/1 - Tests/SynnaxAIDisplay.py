import time
import queue
import threading
import synnax as sy
import nidaqmx
from nidaqmx.constants import AcquisitionType, TerminalConfiguration

# Synnax Setup
client = sy.Synnax(
    host="localhost",
    port=9090,
    username="synnax",
    password="seldon",
    secure=False
)

# Index Channel
time_channel = client.channels.create(
    name="time",
    data_type=sy.DataType.TIMESTAMP,    # required data type for index channels
    is_index=True,
)

# Analog Input Channel
temp_channel = client.channels.create(
    name="AI0",
    data_type=sy.DataType.FLOAT32,
    index=time_channel.key, # indicates that the data in this channel is tied ot the time indicies in time
)