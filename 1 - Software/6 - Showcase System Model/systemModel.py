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

virtual_time = client.channels.create(
    name="virtual_time",
    is_index=True,
    data_type=sy.DataType.TIMESTAMP,
    retrieve_if_name_exists=True 
)

virtual_bottle_press = client.channels.create(
    name = "virtual_bottle_press",
    index = virtual_time.key,
    data_type = sy.DataType.FLOAT32,
    retrieve_if_name_exists = True,
)

virtual_top_tank_press = client.channels.create(
    name = "virtual_top_tank_press",
    index = virtual_time.key,
    data_type = sy.DataType.FLOAT32,
    retrieve_if_name_exists = True,
)

virtual_bot_tank_press = client.channels.create(
    name = "virtual_bot_tank_press",
    index = virtual_time.key,
    data_type = sy.DataType.FLOAT32,
    retrieve_if_name_exists = True,
)

######################################################################################################
# Control Variables
speed_hz = 50
loop = sy.Loop(sy.Rate.HZ * speed_hz)  # 100 Hz update rate

write_channels = [virtual_time] + [virtual_bottle_press] + [virtual_top_tank_press] + [virtual_bot_tank_press]
stream_channels = [f"BangDAQ_do_0_{i}_state" for i in range(4)]

print(write_channels)
print(stream_channels)
######################################################################################################
# Pressure Value Changers
dt = 1 / speed_hz
k = 0.075


######################################################################################################
# Main loop start
print("Main loop starting")

state = {
    "virtual_time": sy.TimeStamp.now(),
    "virtual_bottle_press": 7000,
    "virtual_top_tank_press": 0,
    "virtual_bot_tank_press": 0
}



# Make stateful valve state holder
valve_states = [0, 0, 0, 0]

# Open Synnax interactions
try:
    #Synnax streamer 
    with client.open_streamer(stream_channels) as streamer: 
        #Synnax writer
        with client.open_writer(start=sy.TimeStamp.now(),channels=write_channels,enable_auto_commit=True,) as writer: 
            print("System is init, pressure should be 7000, 0, 0")
            # Main control loop
            while loop.wait():
                # Check for new commands from Synnax console
                frame = streamer.read(timeout=0)
                if frame is not None:
                    #print ("Frame:", frame)
                    for i in range(len(stream_channels)):
                        #print(f"BS {i} and I am {frame.get(stream_channels[i])} and {stream_channels[i]}.")
                        valve_states[i] = int(frame.get(stream_channels[i]))
                        #print("Print:" , valve_states)
                        ###################################
                        if valve_states[3] == 0:
                            #print("ABORT OPEN")
                            if valve_states[0] == 1 or valve_states[1] == 1:
                                #print("BRO THESE ABORT IS OPEN")
                                state["virtual_bottle_press"] -= 20 / speed_hz
                                if(state["virtual_top_tank_press"] > 3 or state["virtual_bot_tank_press"] > 3):
                                    state["virtual_top_tank_press"] -= k * state["virtual_top_tank_press"]**2 / speed_hz
                                    state["virtual_bot_tank_press"] -= k * state["virtual_bot_tank_press"]**2 / speed_hz
                                else:
                                    state["virtual_top_tank_press"] = 0
                                    state["virtual_bot_tank_press"] = 0
                            else:
                                if(state["virtual_top_tank_press"] > 3 or state["virtual_bot_tank_press"] > 3):
                                    state["virtual_top_tank_press"] -= k * state["virtual_top_tank_press"]**2 / speed_hz
                                    state["virtual_bot_tank_press"] -= k * state["virtual_bot_tank_press"]**2 / speed_hz
                                else:
                                    state["virtual_top_tank_press"] = 0
                                    state["virtual_bot_tank_press"] = 0
                        ###################################
                        elif valve_states == [1, 0, 0, 1]:
                            state["virtual_bottle_press"] -= 17.5 / speed_hz
                            state["virtual_top_tank_press"] += 20 / speed_hz
                            state["virtual_bot_tank_press"] += 20 / speed_hz
                            #print("GOON FILL")
                        ###################################
                        elif valve_states == [0, 1, 0, 1]:
                            state["virtual_bottle_press"] -= 10 / speed_hz
                            state["virtual_top_tank_press"] += 50 / speed_hz
                            state["virtual_bot_tank_press"] += 50 / speed_hz
                            #print("CHUD FILL")
                        ###################################
                        elif valve_states == [1, 1, 0, 1]:
                            state["virtual_bottle_press"] -= 30 / speed_hz
                            state["virtual_top_tank_press"] += 75 / speed_hz
                            state["virtual_bot_tank_press"] += 75 / speed_hz
                            #print("FILL THAT BITCH")
                        ###################################
                        elif valve_states == [0, 0, 1, 1]:
                            state["virtual_top_tank_press"] -= 15 / speed_hz
                            state["virtual_bot_tank_press"] -= 15 / speed_hz
                            #print("BLEEEEED")
                        elif valve_states == [1, 0, 1, 1] or valve_states == [0, 1, 1, 1] or valve_states == [1, 1, 1, 1]:
                            state["virtual_bottle_press"] -= 30 / speed_hz
                            state["virtual_top_tank_press"] -= 15 / speed_hz
                            state["virtual_bot_tank_press"] -= 15 / speed_hz
                            #print("BLEEEEED")
                        ###################################
                        elif valve_states == [0, 0, 0, 1]:
                            if(state["virtual_top_tank_press"] > 3 or state["virtual_bot_tank_press"] > 3):
                                state["virtual_top_tank_press"] -= 35 / speed_hz
                                state["virtual_bot_tank_press"] -= 35 / speed_hz
                            else:
                                state["virtual_top_tank_press"] = 0
                                state["virtual_bot_tank_press"] = 0
                            #print("HOLD")
                        ###################################
                        else: 
                            print("CHOPPED STATE")
                    if(state["virtual_bottle_press"] < 500):
                        state["virtual_bottle_press"] = 7000
                    state["virtual_time"] = sy.TimeStamp.now()
                    writer.write(state)
except KeyboardInterrupt:
    print("Keyboard interrupt. Shutting down.")