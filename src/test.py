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

    while len(nodes)==0:
        print("Finding Nodes")
        nodes = conn.find_nodes()

    print(f"Found {len(nodes)} node(s).")
    for node in nodes:
        print(f"[{node.tx_id}] {node.name}")

    source = amfitrack.Device(nodes[0])

    conn.start()
    
    

    cfg = source.config.read_all()
    source.calibrate()

    while True:
        for dev in nodes:
            if dev.packet_available():
                packet = dev.get_packet()
                print(type(packet.payload))
                if type(packet.payload) == amfitrack.payload.EmfImuFrameIdPayload:
                    payload: amfitrack.payload.EmfImuFrameIdPayload = packet.payload
                    print("-----------------------------------")
                    print(packet)
                    print(payload.emf)
                else:
                    print(packet)