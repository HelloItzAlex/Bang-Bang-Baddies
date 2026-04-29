# Import packages
import synnax as sy

######################################################################################################
# Synnax Connect
client = sy.Synnax(
    host="localhost",
    port=9090,
    username="synnax",
    password="seldon",
    secure=False
)

######################################################################################################
# Synnax Channel Create
print("Channel Create")

virtual_bottle_press = client.channels.create(
    name = "virtual_bottle_press",
    data_type = sy.DataType.FLOAT32,
    virtual = True,
    retrieve_if_name_exists = True,
)

virtual_top_tank_press = client.channels.create(
    name = "virtual_top_tank_press",
    data_type = sy.DataType.FLOAT32,
    virtual = True,
    retrieve_if_name_exists = True,
)

virtual_bot_tank_press = client.channels.create(
    name = "virtual_bot_tank_press",
    data_type = sy.DataType.FLOAT32,
    virtual = True,
    retrieve_if_name_exists = True,
)

######################################################################################################
# Control Variables
loop = sy.Loop(sy.Rate.HZ * 100)  # 100 Hz update rate

write_channels = [virtual_bottle_press] + [virtual_top_tank_press] + [virtual_bot_tank_press]
stream_channels = [f"DEV2_do_0_{i}_state" for i in range(4)]

print(write_channels)
print(stream_channels)

######################################################################################################
# Main loop start
print("Main loop starting")

# Initialize state dict
press_states = {
    "virtual_bottle_press": 2000,
    "virtual_top_tank_press": 0,
    "virtual_bot_tank_press": 0
}

# Open Synnax interactions
try:
    #Synnax streamer 
    with client.open_streamer(stream_channels) as streamer: 
        #Synnax writer
        with client.open_writer(start=sy.TimeStamp.now(),channels=write_channels,enable_auto_commit=True,) as writer: 
            print("System is init, pressure should be 2000, 0, 0")
            # Main control loop
            while loop.wait():
                #print("Waiting")
                # Check for new commands from Synnax console
                frame = streamer.read(timeout=0)
                if frame is not None:
                    #print ("Frame:", frame)
                    if state[stateNameList[i]] == 0:
                        writer.write(press_states)
except KeyboardInterrupt:
    print("Keyboard interrupt. Shutting down.")