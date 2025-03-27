
#!/usr/bin/python3

import asyncio
from bleak import BleakClient, BleakScanner
from paho.mqtt import client as mqtt_client
import json
import sys
import getopt
import os
import time
import platform
from loguru import logger

# Configuration de loguru avec une meilleure structuration et couleurs
logger.remove()  # Supprimer le handler par défaut
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
    colorize=True
)

'''
Very basic attempt to just report Solarflow Hub's stats to mqtt for local long-term tests
'''

SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
SF_NOTIFY_CHAR = "0000c305-0000-1000-8000-00805f9b34fb"

# Configuration environment variables
WIFI_PWD = os.environ.get('WIFI_PWD', None)
WIFI_SSID = os.environ.get('WIFI_SSID', None)
SF_DEVICE_ID = os.environ.get('SF_DEVICE_ID', None)
SF_PRODUCT_ID = os.environ.get('SF_PRODUCT_ID', 'ja72U0ha')
GLOBAL_INFO_POLLING_INTERVAL = int(os.environ.get('GLOBAL_INFO_POLLING_INTERVAL', 60))
mqtt_user = os.environ.get('MQTT_USER', None)
mqtt_pwd = os.environ.get('MQTT_PWD', None)
mq_client: mqtt_client = None
bt_client: BleakClient = None
BT_SCAN_TIMEOUT = int(os.environ.get('BT_SCAN_TIMEOUT', 20))  # Durée de scan Bluetooth en secondes

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.success("✅ Connected to MQTT Broker!")
    else:
        logger.error(f"❌ Failed to connect to MQTT, return code {rc}")

def local_mqtt_connect(broker, port):
    """🔄 Connect to MQTT broker with configured credentials"""
    try:
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id="solarflow-bt")
        if mqtt_user is not None and mqtt_pwd is not None:
            logger.debug(f"Using MQTT authentication with user {mqtt_user}")
            client.username_pw_set(mqtt_user, mqtt_pwd)
        client.connect(broker, port)
        client.on_connect = on_connect
        client.loop_start()  # Démarrer la boucle en arrière-plan
        return client
    except Exception as e:
        logger.error(f"❌ MQTT connection failed: {e}")
        return None

async def getInfo(client):
    """📊 Get device info from Solarflow hub"""
    current_time = int(time.time())
    info_cmd = {"messageId": "none", "method": "getInfo", "timestamp": str(current_time)}
    properties_cmd = {"method": "read", "timestamp": str(current_time), "messageId": "none", "properties": ["getAll"]}

    try:
        logger.debug("Sending getInfo command")
        b = bytearray()
        b.extend(map(ord, json.dumps(info_cmd)))
        await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
    except Exception as e:
        logger.error(f"❌ Getting device Info failed: {e}")

    try:
        logger.debug("Sending getAll properties command")
        b = bytearray()
        b.extend(map(ord, json.dumps(properties_cmd)))
        await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
    except Exception as e:
        logger.error(f"❌ Getting device properties failed: {e}")

async def set_IoT_Url(client, broker, port, ssid, deviceid):
    """🌐 Configure the hub's IoT connection settings"""
    global mq_client, SF_PRODUCT_ID
    c1 = {
        'iotUrl': broker,
        'messageId': '1002',
        'method': 'token',
        'password': WIFI_PWD,
        'ssid': ssid,
        'timeZone': 'GMT+02:00',
        'token': 'abcdefgh'
    }
    cmd1 = json.dumps(c1)
    cmd2 = '{"messageId":"1003","method":"station"}'

    reply = '{"messageId":123,"timestamp":' + str(int(time.time())) + ',"params":{"token":"abcdefgh","result":0}}'

    try:
        b = bytearray()
        b.extend(map(ord, cmd1))
        logger.info(f"📤 Setting IoT URL: {cmd1}")
        await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
    except Exception as e:
        logger.error(f"❌ Setting reporting URL failed: {e}")

    try:
        b = bytearray()
        b.extend(map(ord, cmd2))
        logger.info("📤 Setting WiFi station mode")
        await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
    except Exception as e:
        logger.error(f"❌ Setting WiFi Mode failed: {e}")

    if mq_client:
        logger.info(f"📤 Publishing register reply to MQTT")
        mq_client.publish(f'iot/{SF_PRODUCT_ID}/{deviceid}/register/replay', reply, retain=True)

def handle_rx(BleakGATTCharacteristic, data: bytearray):
    """📩 Handle data received from Bluetooth device"""
    global mq_client, SF_PRODUCT_ID, SF_DEVICE_ID
    try:
        payload = json.loads(data.decode("utf8"))
        
        # Format pretty JSON output to improve readability
        logger.debug(f"📥 Received: {json.dumps(payload, indent=2)}")

        if "method" in payload and payload["method"] == "getInfo-rsp":
            logger.success(f"✅ Device ID: {payload['deviceId']}")
            logger.success(f"✅ Device SN: {payload['deviceSn']}")

        if mq_client:
            if "properties" in payload:
                props = payload["properties"]
                logger.debug(f"📊 Properties received: {len(props)} items")
                
                for prop, val in props.items():
                    # Formatter les valeurs pour un affichage plus lisible
                    if isinstance(val, (int, float)):
                        logger.debug(f"  {prop}: {val}")
                    mq_client.publish(f'solarflow-hub/telemetry/{prop}', val)

                # also report whole state to mqtt
                mq_client.publish(f"{SF_PRODUCT_ID}/{SF_DEVICE_ID}/state", json.dumps(payload["properties"]))

            if "packData" in payload:
                packdata = payload["packData"]
                if len(packdata) > 0:
                    logger.info(f"🔋 Battery packs: {len(packdata)}")
                    for pack in packdata:
                        sn = pack.pop('sn')
                        logger.debug(f"  Pack SN: {sn}")
                        for prop, val in pack.items():
                            mq_client.publish(f'solarflow-hub/telemetry/batteries/{sn}/{prop}', val)
    except json.JSONDecodeError:
        logger.error(f"❌ Invalid JSON received: {data.decode('utf8', errors='replace')}")
    except Exception as e:
        logger.error(f"❌ Error handling received data: {e}")
        logger.debug(f"Raw data: {data}")

async def discover_device():
    """🔍 Enhanced device discovery with detailed Bluetooth information"""
    global SF_PRODUCT_ID
    
    product_map = {
        '73bkTV': "zenp",  # Hub1200
        'A8yh63': "zenh",  # Hub2000
        'yWF7hV': "zenr",  # AIO2400
        'ja72U0ha': "zene", # Hyper2000
        '8bM93H': "zenf"
    }
    
    product_class = product_map.get(SF_PRODUCT_ID, "zen")
    os_platform = platform.system()
    
    logger.info(f"🔍 Scanning for Zendure device with prefix: {product_class} on {os_platform}")
    logger.info(f"⌛ Scan timeout: {BT_SCAN_TIMEOUT} seconds")
    
    # Check bluetooth status on Linux-based systems
    if os_platform in ('Linux', 'Darwin'):
        try:
            # Add debug info about bluetooth adapters
            import subprocess
            result = subprocess.run(['hciconfig'], capture_output=True, text=True)
            logger.debug(f"Bluetooth adapters:\n{result.stdout}")
            
            # Check if bluetooth service is running
            if os_platform == 'Linux':
                result = subprocess.run(['systemctl', 'status', 'bluetooth'], capture_output=True, text=True)
                if "Active: active (running)" in result.stdout:
                    logger.debug("✅ Bluetooth service is running")
                else:
                    logger.warning("⚠️ Bluetooth service may not be running properly")
                    logger.debug(result.stdout)
        except Exception as e:
            logger.error(f"❌ Error checking Bluetooth status: {e}")
    
    # Scan for all devices to debug
    all_devices = await BleakScanner.discover(timeout=BT_SCAN_TIMEOUT)
    if all_devices:
        logger.debug(f"Found {len(all_devices)} Bluetooth devices:")
        for i, d in enumerate(all_devices):
            logger.debug(f"  Device {i+1}: {d.name or 'Unknown'} ({d.address})")
    else:
        logger.warning("❌ No Bluetooth devices found at all! Check your Bluetooth adapter.")
        
    # Try both methods to find device
    device = None
    
    # Method 1: Find by filter (original method)
    try:
        logger.info("🔍 Trying to find device by name filter...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name and d.name.lower().startswith(product_class),
            timeout=BT_SCAN_TIMEOUT
        )
    except Exception as e:
        logger.error(f"❌ Error during name filter scan: {e}")
    
    # Method 2: If first method failed, try to find device manually in all discovered devices
    if not device:
        logger.info("🔍 Trying alternative method to find device...")
        for d in all_devices:
            if d.name and d.name.lower().startswith(product_class):
                device = d
                logger.success(f"✅ Found device using alternative method: {d.name} ({d.address})")
                break
    
    return device

async def run(broker=None, port=None, info_only: bool = False, connect: bool = False, 
              disconnect: bool = False, ssid=None, deviceid=None):
    """🚀 Main execution function"""
    global mq_client
    global bt_client
    global SF_PRODUCT_ID
    
    # Start broker connection if requested
    if broker and port:
        logger.info(f"🔄 Connecting to MQTT broker {broker}:{port}")
        mq_client = local_mqtt_connect(broker, port)
    
    # Find the device
    device = await discover_device()

    if device:
        logger.success(f"✅ Found device: {device.name} ({device.address})")
        
        try:
            # Connect to device
            logger.info(f"🔌 Connecting to {device.name}...")
            async with BleakClient(device) as bt_client:
                svcs = bt_client.services
                logger.debug("Services available:")
                for service in svcs:
                    logger.debug(f"  {service}")

                # Handle disconnect request
                if disconnect and broker and port and ssid and SF_DEVICE_ID:
                    logger.info("🔄 Setting to disconnect from Zendure cloud")
                    await set_IoT_Url(bt_client, broker, port, ssid, SF_DEVICE_ID)
                    logger.success("✅ Disconnected from Zendure cloud - should report to local MQTT now")
                    await asyncio.sleep(30)
                    return

                # Handle connect request
                if connect and ssid:
                    logger.info("🔄 Setting to reconnect to Zendure cloud")
                    await set_IoT_Url(bt_client, "mq.zen-iot.com", 1883, ssid, SF_DEVICE_ID)
                    logger.success("✅ Reconnected to Zendure cloud")
                    await asyncio.sleep(30)
                    return

                # Handle info only request
                if info_only and broker is None:
                    logger.info("ℹ️ Running in info-only mode")
                    await bt_client.start_notify(SF_NOTIFY_CHAR, handle_rx)
                    await getInfo(bt_client)
                    await asyncio.sleep(20)
                    await bt_client.stop_notify(SF_NOTIFY_CHAR)
                    return
                
                # Run continuous mode
                else:
                    logger.info(f"📊 Running in continuous polling mode (interval: {GLOBAL_INFO_POLLING_INTERVAL}s)")
                    getinfo = True
                    while True:
                        try:
                            await bt_client.start_notify(SF_NOTIFY_CHAR, handle_rx)
                            # fetch global info based on interval
                            if getinfo:
                                await getInfo(bt_client)
                                getinfo = False
                            # Smart sleep with return value
                            getinfo = await asyncio.sleep(GLOBAL_INFO_POLLING_INTERVAL, True)
                        except Exception as e:
                            logger.error(f"❌ Error in polling loop: {e}")
                            # Try to restore connection
                            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"❌ Connection to device failed: {e}")
    else:
        logger.error("❌ No Solarflow device found!")
        logger.warning("""
📋 Troubleshooting steps:
  1. Move closer to the hub
  2. Reset your bluetooth connection (sudo systemctl restart bluetooth)
  3. Make sure bluetooth is enabled (bluetoothctl power on)
  4. Restart the Solarflow Hub
  5. Disconnect any mobile Apps currently connected to the hub
  6. Try increasing scan timeout with BT_SCAN_TIMEOUT environment variable
  7. On Ubuntu/Linux systems, ensure you have proper permissions (try with sudo)
  8. Check if btmon shows any BT activity when scanning
""")

def main(argv):
    """🚀 Main entry point with improved command line handling"""
    global mqtt_user, mqtt_pwd, SF_DEVICE_ID, BT_SCAN_TIMEOUT, GLOBAL_INFO_POLLING_INTERVAL
    
    ssid = None
    mqtt_broker = None
    mqtt_port = None
    connect = disconnect = info_only = debug_mode = False
    
    # Improved command line argument handling
    try:
        opts, args = getopt.getopt(argv, "hidb:u:p:w:ct:l:", [
            "help", "info", "disconnect", "mqtt_broker=", "mqtt_user=", 
            "mqtt_pwd=", "wifi=", "connect", "timeout=", "loglevel=", "debug"
        ])
    except getopt.GetoptError as e:
        logger.error(f"❌ Command line error: {e}")
        print_help()
        sys.exit(2)
        
    for opt, arg in opts:
        if opt in ("-h", "--help"):
            print_help()
            sys.exit(0)
        elif opt in ("-i", "--info"):
            info_only = True
            disconnect = connect = False
        elif opt in ("-d", "--disconnect"):
            disconnect = True
            connect = False
        elif opt in ("-w", "--wifi"):
            ssid = arg
        elif opt in ("-b", "--mqtt_broker"):
            parts = arg.split(':')
            mqtt_broker = parts[0]
            mqtt_port = int(parts[1]) if len(parts) > 1 else 1883
        elif opt in ("-u", "--mqtt_user"):
            mqtt_user = arg
        elif opt in ("-p", "--mqtt_pwd"):
            mqtt_pwd = arg
        elif opt in ("-c", "--connect"):
            connect = True
            disconnect = False
        elif opt in ("-t", "--timeout"):
            try:
                BT_SCAN_TIMEOUT = int(arg)
                logger.info(f"🕒 Setting Bluetooth scan timeout to {BT_SCAN_TIMEOUT} seconds")
            except ValueError:
                logger.error(f"❌ Invalid timeout value: {arg}")
        elif opt in ("-l", "--loglevel"):
            # Set log level
            try:
                logger.remove()
                logger.add(
                    sys.stdout,
                    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
                    level=arg.upper(),
                    colorize=True
                )
                logger.success(f"✅ Log level set to {arg.upper()}")
            except ValueError:
                logger.error(f"❌ Invalid log level: {arg}")
                logger.info("Valid levels: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        elif opt == "--debug":
            debug_mode = True
            # Set to DEBUG level
            logger.remove()
            logger.add(
                sys.stdout,
                format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
                level="DEBUG",
                colorize=True
            )
            logger.success("✅ Debug mode activated")

    # Print environment info in debug mode
    if debug_mode:
        logger.debug(f"🔧 Platform: {platform.platform()}")
        logger.debug(f"🔧 Python version: {platform.python_version()}")
        
        # Try to get Bluetooth info
        if platform.system() in ('Linux', 'Darwin'):
            try:
                import subprocess
                result = subprocess.run(['bluetoothctl', 'show'], capture_output=True, text=True)
                logger.debug(f"🔧 Bluetooth controller info:\n{result.stdout}")
            except Exception as e:
                logger.debug(f"❌ Error getting Bluetooth info: {e}")

    # Validation checks for disconnect mode
    if disconnect:
        errors = []
        if ssid is None:
            errors.append("WiFi SSID (-w) is required!")
        if mqtt_broker is None:
            errors.append("Local MQTT broker (-b) is required!")
        if WIFI_PWD is None:
            errors.append(f'WiFi password for SSID "{ssid}" is required (set WIFI_PWD environment variable)')
        if SF_DEVICE_ID is None:
            errors.append("Device ID is required (set SF_DEVICE_ID environment variable)")
        
        if errors:
            logger.error("❌ Configuration errors:")
            for error in errors:
                logger.error(f"  - {error}")
            logger.info("ℹ️ Run with -h for usage information")
            sys.exit(1)
            
        logger.info("🔄 Disconnecting Solarflow Hub from Zendure Cloud...")

    # Info for connect mode
    if connect:
        if ssid is None:
            logger.error("❌ WiFi SSID (-w) is required for connect mode!")
            sys.exit(1)
        logger.info("🔄 Connecting Solarflow Hub back to Zendure Cloud...")
    
    # Start async run
    try:
        asyncio.run(run(broker=mqtt_broker, port=mqtt_port, info_only=info_only, 
                        connect=connect, disconnect=disconnect, ssid=ssid,
                        deviceid=SF_DEVICE_ID))
    except KeyboardInterrupt:
        logger.info("👋 Program interrupted by user. Exiting...")
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred: {e}")
        if debug_mode:
            import traceback
            logger.debug(f"Traceback:\n{traceback.format_exc()}")

def print_help():
    """📋 Display improved help information"""
    print("""
🌞 SOLARFLOW BLUETOOTH MANAGER 🔋
Usage: solarflow-bt-manager.py [OPTIONS]

Options:
  -h, --help                Show this help message
  -i, --info                Print hub information and exit
  -d, --disconnect          Disconnect hub from Zendure cloud
  -c, --connect             Connect hub back to Zendure cloud
  -b, --mqtt_broker HOST:PORT  Local MQTT broker address
  -u, --mqtt_user USER      MQTT username
  -p, --mqtt_pwd PASSWORD   MQTT password
  -w, --wifi SSID           WiFi SSID for the hub
  -t, --timeout SECONDS     Bluetooth scan timeout (default: 20)
  -l, --loglevel LEVEL      Set log level (DEBUG, INFO, WARNING, ERROR)
  --debug                   Enable debug mode with detailed logging

Environment Variables:
  WIFI_PWD                  Password for the WiFi SSID
  SF_DEVICE_ID              Your device ID (required for disconnect/connect)
  SF_PRODUCT_ID             Product ID (73bkTV for Hub1200, A8yh63 for Hub2000, etc.)
  GLOBAL_INFO_POLLING_INTERVAL  Polling interval in seconds (default: 60)
  BT_SCAN_TIMEOUT           Bluetooth scan timeout in seconds (default: 20)
  MQTT_USER                 MQTT username (alternative to -u)
  MQTT_PWD                  MQTT password (alternative to -p)
  
Examples:
  # Just show information about your hub:
  ./solarflow-bt-manager.py -i
  
  # Disconnect from Zendure cloud and connect to local MQTT:
  export WIFI_PWD='your_wifi_password'
  export SF_DEVICE_ID='your_device_id'
  ./solarflow-bt-manager.py -d -w YourWiFiName -b 192.168.1.100:1883
  
  # Set longer BT scan timeout with debug logging:
  ./solarflow-bt-manager.py -i -t 60 --debug
""")

if __name__ == '__main__':
    main(sys.argv[1:])