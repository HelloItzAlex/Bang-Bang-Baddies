import nidaqmx
from nidaqmx.constants import LineGrouping
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
numRelayChannels = 8

# Time channel for state writes (shared)
NI9485TimeChannel = client.channels.create(
    name="NI9485Time",
    is_index=True,
    retrieve_if_name_exists=True
)

# Command channels & times
cmdTimeChannels = []
cmdChannels = []
for i in range(numRelayChannels):
    cmdTimeChannels.append(client.channels.create(
        name=f"NI_9485_CH{i}_CMD_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    ))
    cmdChannels.append(client.channels.create(
        name=f"NI_9485_CH{i}_CMD",
        data_type=sy.DataType.UINT8,
        index=cmdTimeChannels[i].key,
        retrieve_if_name_exists=True
    ))

# State channels
stateChannels = []
for i in range(numRelayChannels):
    stateChannels.append(client.channels.create(
        name=f"NI_9485_CH{i}_STATE",
        data_type=sy.DataType.UINT8,
        index=NI9485TimeChannel.key,
        retrieve_if_name_exists=True
    ))
######################################################################################################
# Lockout Channel Create
lockoutNames = ["MasterArm", "MPVALockout", "VentsLockout"]
numLockouts = range(len(lockoutNames))

# Time channel for state writes (shared)
LockoutTimeChannel = client.channels.create(
    name="LockoutTime",
    is_index=True,
    retrieve_if_name_exists=True
)

# Command channels & times
lockoutCMDTimeChannels = []
lockoutCMDChannels = []
for i in range(numLockouts):
    lockoutCMDTimeChannels.append(client.channels.create(
        name=f"{lockoutNames[i]}_CMD_TIME",
        is_index=True,
        data_type=sy.DataType.TIMESTAMP,
        retrieve_if_name_exists=True
    ))
    lockoutCMDChannels.append(client.channels.create(
        name=f"{lockoutNames[i]}_CMD",
        data_type=sy.DataType.UINT8,
        index=cmdTimeChannels[i].key,
        retrieve_if_name_exists=True
    ))

# State channels
lockoutSTATEChannels = []
for i in range(numLockouts):
    lockoutSTATEChannels.append(client.channels.create(
        name=f"{lockoutNames[i]}_STATE",
        data_type=sy.DataType.UINT8,
        index=NI9485TimeChannel.key,
        retrieve_if_name_exists=True
    ))

######################################################################################################
# --- Control Variables ---
loop = sy.Loop(sy.Rate.HZ * 10)  # 10 Hz update rate

writeChannels = [NI9485TimeChannel] + stateChannels + lockoutSTATEChannels

cmdNameList = [f"NI_9485_CH{i}_CMD" for i in range(numRelayChannels)]
cmdNameList += [f"{name}_CMD" for name in lockoutNames]

stateNameList = [f"NI_9485_CH{i}_STATE" for i in range(numRelayChannels)]
stateNameList += [f"{name}_STATE" for name in lockoutNames]

######################################################################################################
print("Starting module multi-line control...")

# Initialize state dict
state = {
    "NI9485Time": sy.TimeStamp.now(),
    **{name: 0 for name in stateNameList}
}
writeStates = [False]*8
writeLockoutStates = [False]*numLockouts
try:
    with nidaqmx.Task() as ni_task,\
        client.open_streamer(cmdNameList) as streamer,\
        client.open_writer(start=sy.TimeStamp.now(),channels=writeChannels,enable_auto_commit=True,) as writer:
        ni_task.do_channels.add_do_chan(
            "cDAQ1Mod5/port0/line0:7",
            line_grouping=LineGrouping.CHAN_PER_LINE
        )
        ni_task.start()
        ni_task.write(writeStates)
        print("Relays initialized to OFF")
        # Initialize state dict
        writer.write(state)
    
        print("System ready! Valves should be closed")
        
        # Main control loop
        while loop.wait():
            # Check for new commands from Synnax console
            frame = streamer.read(timeout=0)
            if frame is not None:
                #print ("Frame:", frame)
                print(writeStates)

                #Check to see which command was sent
                


                for i in range(len(cmdChannels)):
                    cmdChannels[i] = frame.get(cmdNameList[i])
                    #print("Tempchannel1:", cmdChannels[i])
                    if len(cmdChannels[i]) > 0:
                        state[stateNameList[i]] = cmdChannels[i][-1]
                        writeStates[i] = bool(cmdChannels[i][-1])
                        print(cmdChannels[i][-1])
                state["NI9485Time"] = sy.TimeStamp.now()
                # Update hardware
                print(writeStates)
                print("State:",state)
                ni_task.write(writeStates)
                writer.write(state)
except KeyboardInterrupt:
    print("Shutting down...")