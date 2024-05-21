import array
import sys
import hid
import multiprocessing as mp
import time
import hashlib
import enum
import atexit
from typing import List, Optional
from .packet import Packet, PacketDestination
from .common_payload import RequestDeviceIdPayload, ReplyDeviceIdPayload, RequestDeviceNamePayload, ReplyDeviceNamePayload
from .node import Node
from .connection import Connection

USB_HID_REPORT_LENGTH = 64


class USBConnection(Connection):
    """An implementation of :class:`amfiprot.Connection` used to connect to USB HID devices."""
    MAX_PAYLOAD_SIZE = 54  # 1 byte needed for CRC

    def __init__(self, vendor_id: int, product_id: int, serial_number: str = None):
        """ If no serial number is given, the first device that matches vendor_id and product_id is used. """
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.usb_serial_number = serial_number

        self.usb_device = get_matching_device(vendor_id, product_id, serial_number)

        if self.usb_device is None:
            raise ConnectionError("Could not connect to device!")

        self.receive_process: mp.Process = None
        self.transmit_process: mp.Process = None
        self.usb_task: mp.Process = None
        self.nodes: List[Node] = []
        self.transmit_queue: mp.Queue = mp.Queue()
        self.global_receive_queue: mp.Queue = mp.Queue()
        self.usb_connection_lost: mp.Event = mp.Event()
        self.node_update_queue: mp.Queue = mp.Queue()

    def __del__(self):
        try:
            self.stop()
        except AttributeError:
            pass  # Processes not started

    @classmethod
    def discover(cls):
        devices = hid.enumerate()
        device_list = []

        for device in devices:
            try:
                if device['manufacturer_string'] is None or device['product_string'] is None:
                    raise ValueError("Unknown device")

                try:
                    serial_number = device['serial_number']
                except Exception:
                    serial_number = None

                device_info = {'vid': device['vendor_id'], 'pid': device['product_id'], 'manufacturer': device['manufacturer_string'], 'product': device['product_string'], 'serial_number': serial_number}
                device_list.append(device_info)
            except Exception:
                continue

        return device_list

    def find_nodes(self) -> List[Node]:
        # TODO: Clean this method. Implement helper functions for blocking send/receive

        # Create 'request device id' packet
        payload = RequestDeviceIdPayload()
        packet = Packet.from_payload(payload, destination_id=PacketDestination.BROADCAST)

        # Send packet via USB
        bytes_to_transmit: array.array = array.array('B', [1])  # USB header Report ID, packet length only required on IN packets (???)
        bytes_to_transmit.extend(packet.to_bytes())

        while len(bytes_to_transmit) < 64:
            bytes_to_transmit.append(0)

        self.usb_device.write(bytes_to_transmit)

        start_time = time.time()

        rx_packets = []
        while time.time() - start_time < 1:
            data = None
            try:
                data_raw = self.usb_device.read(64)
                data = array.array('B', data_raw)
            except:
                #print("USB timed out while waiting for Device ID reply packets.")
                continue

            if len(data) == 0:  # Extra safe guard for Linux (or previous libusb version??)
                continue

            rx_packet = Packet(data[2:])
            #print(rx_packet)

            if type(rx_packet.payload) == ReplyDeviceIdPayload:
                rx_packets.append(rx_packet)

        nodes = []

        for packet in rx_packets:
            if type(packet.payload) == ReplyDeviceIdPayload:
                uuids = [node.uuid for node in nodes]
                if packet.payload.uuid not in uuids:
                    node = Node(tx_id=packet.payload.tx_id, uuid=packet.payload.uuid, connection=self)
                    nodes.append(node)

        # Request device name for all found nodes
        for node in nodes:
            payload = RequestDeviceNamePayload()
            packet = Packet.from_payload(payload, destination_id=node.tx_id)

            # Send packet via USB
            bytes_to_transmit: array.array = array.array('B', [1])  # USB header Report ID, packet length only required on IN packets (???)
            bytes_to_transmit.extend(packet.to_bytes())

            while len(bytes_to_transmit) < 64:
                bytes_to_transmit.append(0)

            self.usb_device.write(bytes_to_transmit)

            start_time = time.time()

            while time.time() - start_time < 1:
                data = None

                try:
                    data = array.array('B', self.usb_device.read(64, timeout_ms=1000))
                except:
                    # print("USB timed out while waiting for Device ID reply packets.")
                    continue

                rx_packet = Packet(data[2:])
                # print(rx_packet)

                if type(rx_packet.payload) == ReplyDeviceNamePayload:
                    node.name = rx_packet.payload.name
                    break


        # Restart connection if nodes list changed
        if nodes_changed(nodes, self.nodes):
            self.nodes = nodes
            # Send updated node list to RX/TX processes (if started)
            if self.receive_process is not None and self.transmit_process is not None:
                # HACK: just restart connection using new self.nodes list
                self.stop()
                self.start()
            # print("New node detected!")

        return self.nodes

    def enqueue_packet(self, packet: Packet):
        self.transmit_queue.put(packet)

    def max_payload_size(self) -> int:
        return self.MAX_PAYLOAD_SIZE

    def start(self):
        usb_device_hash = generate_device_hash(self.usb_device)
        #usb.util.dispose_resources(self.usb_device)
        del self.usb_device

        atexit.register(connection_exit_handler, self)

        # Create rx process
        tx_ids = [node.tx_id for node in self.nodes]
        rx_queues = [node.receive_queue for node in self.nodes]

        self.usb_task = mp.Process(target=usb_task, args=(usb_device_hash, tx_ids, rx_queues, self.transmit_queue, self.global_receive_queue, self.node_update_queue))
        self.usb_task.start()
        time.sleep(1)  # To allow processes to start up

    def stop(self):
        # TODO: Send stop request to task and wait for acknowledge (allows outbound packets to be sent before stopping)

        if self.usb_task is not None:
            if self.usb_task.is_alive():
                time.sleep(1)  # To allow pending tx packets to be sent
                self.usb_task.terminate()
                self.usb_task.join()

    def refresh(self):
        #rx_queues = [node.receive_queue for node in self.nodes]  # Cannot send queues in a queue to other process
        tx_ids = [node.tx_id for node in self.nodes]
        self.node_update_queue.put({'tx_ids': tx_ids})

    def __str__(self):
        bus = self.usb_device.bus
        address = self.usb_device.address

        vendor_id = self.usb_device.idVendor
        product_id = self.usb_device.idProduct

        manufacturer_string_index = self.usb_device.iManufacturer
        product_string_index = self.usb_device.iProduct
        serial_number_string_index = self.usb_device.iSerialNumber

        manufacturer = self.usb_device.get_manufacturer_string()
        product = self.usb_device.get_product_string()
        serial_number = self.usb_device.get_serial_number_string()

        return f"{product} ({manufacturer}) on bus {bus} address {address}. VID=0x{vendor_id:04X},"\
               f" PID=0x{product_id:04X}, SN={serial_number}"


class ConnectionState(enum.IntEnum):
    INIT = 0
    CONNECTED = 1
    DISCONNECTED = 2


def usb_task(usb_device_hash, tx_ids, rx_queues: List[mp.Queue], tx_queue: mp.Queue, global_receive_queue: mp.Queue, node_update_queue: mp.Queue):
    IN_ENDPOINT = 0x81
    OUT_ENDPOINT = 0x01
    RETRY_LIMIT = 10

    retry_count = 0
    dev = None

    while dev is None:
        dev = get_usb_device_by_hash(usb_device_hash)
        state = ConnectionState.CONNECTED

        retry_count = retry_count + 1
        if retry_count > RETRY_LIMIT and dev is None:
            raise ConnectionError("Subprocess could not find device.")

    tx_ids_local = tx_ids
    rx_queues_local = rx_queues

    while True:
        if state == ConnectionState.CONNECTED:

            # Send all pending packets
            while not tx_queue.empty():
                tx_packet = tx_queue.get_nowait()

                byte_data = array.array('B', [1])
                byte_data.extend(tx_packet.to_bytes())
                byte_data.extend([0] * (64 - len(byte_data)))
                try:
                    bytes_written = dev.write(byte_data) # TODO: Do something about this timeout! It was 1 ms before in order not to block reading, but sometimes it is not enough time to actually send the packet.
                except:  # TODO: Check disconnect in some other way before getting from tx_queue, because this drops packets!
                    print(f"Could not send packet ({e})")
                    continue

            # Check for tx_id change before receiving
            if not node_update_queue.empty():
                update = node_update_queue.get()
                tx_ids_local = update['tx_ids']

            # Try to receive
            try:
                rx_data = dev.read(USB_HID_REPORT_LENGTH, timeout_ms=0)
                rx_data = array.array('B', rx_data)
            except Exception as e:
                print(f"USB connection lost: {e}")
                state = ConnectionState.DISCONNECTED
                continue

            if len(rx_data) == 0:
                continue

            rx_packet = Packet(rx_data[2:])

            if global_receive_queue.full():
                print("Global receive queue full! Packet discarded.")
            else:
                global_receive_queue.put_nowait(
                    rx_packet)  # TODO: What if queue is full? Should probably only keep newest packets

            # Push packet to correct rx_queue
            try:
                index = tx_ids_local.index(rx_packet.source_id)  # .index returns ValueError if value not found in list
                if rx_queues_local[index].full():
                    print(f"RX queue [TxID {tx_ids_local[index]}] full! Packet discarded.")
                else:
                    rx_queues_local[index].put_nowait(rx_packet)

            except ValueError:
                # print(f"Packet TxID {rx_packet.source_id} does not match any nodes.")
                pass

        elif state == ConnectionState.DISCONNECTED:
            print("Reconnecting...")

            dev = get_usb_device_by_hash(usb_device_hash)

            if dev is not None:
                print("Connection re-established!")
                state = ConnectionState.CONNECTED
            else:
                time.sleep(1)
        else:
            raise ValueError("Invalid state in USB task.")


def connect_usb(vendor_id, product_id, serial_number=None):
    for i in range(3):
        try:
            devices = get_usb_devices(vendor_id, product_id)

            if len(devices) > 0:
                break
            else:
                time.sleep(1)
                continue

        except ValueError:
            print("Device not found. Retrying in 1 sec...")
            time.sleep(1)

    if len(devices) < 1:
        raise ConnectionError("Could not reconnect")

    dev = devices[0]

    if "linux" in sys.platform.lower():
        for config in dev:
            for i in range(config.bNumInterfaces):
                if dev.is_kernel_driver_active(i):
                    dev.detach_kernel_driver(i)

    return dev


def get_usb_devices(vendor_id=0, product_id=0):
    return hid.enumerate(vendor_id, product_id)


def get_serial_number(device) -> int:
    return device.get_serial_number_string()


def get_matching_device(vendor_id, product_id, serial_number):
    if serial_number is None:
        devices = hid.enumerate(vendor_id, product_id)

        if len(devices) == 0:
            raise ConnectionError("No match!")
        device = hid.device()
        device.open(vendor_id, product_id)
        device.set_nonblocking(1)


        if "linux" in sys.platform.lower():
            # print("Host is Linux")
            linux_usb_workaround(device)
        # device.set_configuration()
        return device
    else:
        devices = hid.enumerate(vendor_id, product_id)

        for device in devices:
            if str(device['serial_number']) == serial_number:
                dev = hid.device()
                dev.open(vendor_id, product_id)
                device.set_nonblocking(1)
                return dev
        return None


def generate_device_hash(device) -> str:
    string_to_hash = device.get_serial_number_string()
    encoded_string = string_to_hash.encode('utf-8')

    hash = hashlib.md5()
    hash.update(encoded_string)
    return hash.hexdigest()


def get_usb_device_by_hash(hash: str):
    devices = get_usb_devices()

    if len(devices) > 0:
        for device in devices:
            try:
                dev = hid.device()
                dev.open(device["vendor_id"], device["product_id"])
                dev.set_nonblocking(1)
                current_hash = generate_device_hash(dev)
            except ValueError:
                continue
            except NotImplementedError:
                continue

            if hash == current_hash:

                # if "linux" in sys.platform.lower():
                #     print("Host is Linux!")
                #     for config in device:
                #         for i in range(config.bNumInterfaces):
                #             if device.is_kernel_driver_active(i):
                #                 device.detach_kernel_driver(i)
                # device.set_configuration()

                return dev

    return None


def nodes_changed(list1: List[Node], list2):
    # Check if lengths differ
    if len(list1) != len(list2):
        return True

    # Sort both lists by uuid and compare each element (list equality will not work)
    list1.sort(key=lambda x: x.uuid)
    list2.sort(key=lambda x: x.uuid)

    for (item1, item2) in zip(list1, list2):
        if item1.uuid != item2.uuid:
            return True

    return False


def linux_usb_workaround(device):
    for config in device:
        for i in range(config.bNumInterfaces):
            if device.is_kernel_driver_active(i):
                device.detach_kernel_driver(i)


def connection_exit_handler(connection):
    connection.stop()
