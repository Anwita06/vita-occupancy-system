# websocket_server.py  ←←← SAVE AS THIS NAME
import asyncio
import websockets

connected_clients = set()
arduino_client = None

async def broadcast_message(message: str):
    """Sends a message to all connected web browser clients."""
    print(f"Broadcast → {message}")
    disconnected = set()
    for client in connected_clients:
        try:
            await client.send(message)
        except websockets.ConnectionClosed:
            disconnected.add(client)
        except Exception:
            disconnected.add(client)
    connected_clients.difference_update(disconnected)


async def handler(websocket):
    global arduino_client
    
    print(f"New connection from {websocket.remote_address}")
    
    # Treat all new connections as recipients (browser clients)
    connected_clients.add(websocket)
    print(f"Client connected! Total: {len(connected_clients)}")

    try:
        async for message in websocket:
            # Assume any incoming message is from the Uno client and needs broadcasting
            # The format is expected to be '1:present' or '1:absent'
            await broadcast_message(message)
            
    except websockets.ConnectionClosedOK:
        print(f"Client disconnected gracefully.")
    except websockets.ConnectionClosedError as e:
        print(f"Client disconnected with error: {e}")
    except Exception as e:
        print(f"Error in handler: {e}")
    finally:
        # Remove client on disconnect
        if websocket in connected_clients:
            connected_clients.remove(websocket)
            print(f"Client disconnected. Total: {len(connected_clients)}")


async def main():
    print("WebSocket Server RUNNING on ws://0.0.0.0:8765")
    async with websockets.serve(handler, "0.0.0.0", 8765):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())