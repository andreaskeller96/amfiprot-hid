import amfiprot
import amfiprot_amfitrack as amfitrack

VENDOR_ID = 0xC17
PRODUCT_ID_SENSOR = 0xD12
PRODUCT_ID_SOURCE = 0xD01

if __name__ == "__main__":
    conn = None
    try:
        conn = amfiprot.USBConnection(VENDOR_ID, PRODUCT_ID_SENSOR)
    except:
        try:
            conn = amfiprot.USBConnection(VENDOR_ID, PRODUCT_ID_SOURCE)
        except:
            print("No Amfitrack device found")
            exit()
            
    nodes = conn.find_nodes()

    print(f"Found {len(nodes)} node(s).")
    for node in nodes:
        print(f"[{node.tx_id}] {node.name}")

    dev = amfitrack.Device(nodes[0])
    sensor = None
    if len(nodes)>1:
        sensor = amfitrack.Device(nodes[1])
    conn.start()
    
    cfg = dev.config.read_all()
    dev.calibrate()

    while True:
        if not sensor and dev.packet_available():
            packet = dev.get_packet()
            if type(packet.payload) == amfitrack.payload.EmfImuFrameIdPayload:
                payload: amfitrack.payload.EmfImuFrameIdPayload = packet.payload
                print(payload.emf)
            else:
                print(packet)
        if sensor is not None and sensor.packet_available():
            packet = sensor.get_packet()
            if type(packet.payload) == amfitrack.payload.EmfImuFrameIdPayload:
                payload: amfitrack.payload.EmfImuFrameIdPayload = packet.payload
                print(payload.emf)
            else:
                print(packet)