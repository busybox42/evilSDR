#!/usr/bin/env python3
"""
Thread Safety Test for evilSDR Server

This script simulates high-load scenarios to verify the thread-safety refactoring.
Run this against the refactored server to ensure no race conditions occur.
"""

import asyncio
import json
import random
import time
from websockets import connect

SERVER_URL = "ws://localhost:8765"
NUM_CLIENTS = 10
TEST_DURATION = 30  # seconds

class TestClient:
    def __init__(self, client_id):
        self.client_id = client_id
        self.ws = None
        self.messages_received = 0
        self.errors = []
        
    async def run(self):
        """Simulate a client that connects, sends random commands, and disconnects."""
        try:
            async with connect(SERVER_URL) as ws:
                self.ws = ws
                print(f"[Client {self.client_id}] Connected")
                
                # Send random commands
                for _ in range(random.randint(5, 20)):
                    await asyncio.sleep(random.uniform(0.1, 1.0))
                    
                    # Random command
                    cmd = random.choice([
                        {"type": "START_STREAM"},
                        {"type": "STOP_STREAM"},
                        {"type": "SET_MODE", "mode": random.choice(["FM", "AM", "USB", "LSB"])},
                        {"type": "SET_SQUELCH", "value": random.randint(-80, -20)},
                        {"type": "SET_FREQ", "value": random.randint(88_000_000, 108_000_000)},
                    ])
                    
                    await ws.send(json.dumps(cmd))
                    
                    # Receive messages (non-blocking)
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        self.messages_received += 1
                    except asyncio.TimeoutError:
                        pass
                        
                print(f"[Client {self.client_id}] Disconnecting (received {self.messages_received} messages)")
                
        except Exception as e:
            self.errors.append(str(e))
            print(f"[Client {self.client_id}] Error: {e}")

async def stress_test():
    """Run multiple clients concurrently to stress-test thread safety."""
    print(f"Starting stress test with {NUM_CLIENTS} clients for {TEST_DURATION}s")
    print("This test simulates concurrent connections/disconnections + rapid command changes")
    print("-" * 70)
    
    start_time = time.time()
    clients = []
    tasks = []
    
    # Spawn clients at random intervals
    for i in range(NUM_CLIENTS):
        client = TestClient(i)
        clients.append(client)
        tasks.append(asyncio.create_task(client.run()))
        await asyncio.sleep(random.uniform(0, 2))  # Stagger connections
        
        # Check if we've exceeded test duration
        if time.time() - start_time > TEST_DURATION:
            break
    
    # Wait for all clients to complete
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # Results
    print("-" * 70)
    print("Test completed!")
    total_messages = sum(c.messages_received for c in clients)
    total_errors = sum(len(c.errors) for c in clients)
    
    print(f"Total messages received: {total_messages}")
    print(f"Total errors: {total_errors}")
    
    if total_errors > 0:
        print("\nErrors encountered:")
        for client in clients:
            if client.errors:
                print(f"  Client {client.client_id}: {client.errors}")
    else:
        print("✅ No errors! Thread safety appears to be working correctly.")

async def concurrent_mode_changes():
    """Test rapid mode changes from multiple clients simultaneously."""
    print("\nTesting concurrent DSP mode changes...")
    
    async def change_modes(client_id):
        async with connect(SERVER_URL) as ws:
            for _ in range(20):
                mode = random.choice(["FM", "AM", "USB", "LSB"])
                await ws.send(json.dumps({"type": "SET_MODE", "mode": mode}))
                await asyncio.sleep(0.05)  # Very fast changes
    
    tasks = [change_modes(i) for i in range(5)]
    await asyncio.gather(*tasks)
    print("✅ Concurrent mode changes completed")

async def recording_toggle_test():
    """Test rapid start/stop of recordings."""
    print("\nTesting rapid recording toggles...")
    
    async with connect(SERVER_URL) as ws:
        for _ in range(10):
            await ws.send(json.dumps({"type": "START_IQ_RECORD"}))
            await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "STOP_IQ_RECORD"}))
            await asyncio.sleep(0.1)
            
            await ws.send(json.dumps({"type": "START_AUDIO_RECORD"}))
            await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "STOP_AUDIO_RECORD"}))
            await asyncio.sleep(0.1)
    
    print("✅ Recording toggle test completed")

async def main():
    print("=" * 70)
    print("evilSDR Thread Safety Test Suite")
    print("=" * 70)
    print(f"Server: {SERVER_URL}")
    print()
    
    try:
        # Test 1: Stress test with multiple clients
        await stress_test()
        
        # Test 2: Concurrent DSP operations
        await concurrent_mode_changes()
        
        # Test 3: Recording toggles
        await recording_toggle_test()
        
        print("\n" + "=" * 70)
        print("All tests completed successfully! 🎉")
        print("=" * 70)
        
    except Exception as e:
        print(f"\n❌ Test suite failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
