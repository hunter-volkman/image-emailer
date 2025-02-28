import asyncio
import os
from viam.robot.client import RobotClient
from viam.rpc.dial import DialOptions

async def restart_module():
    # Load credentials from environment variables set by Viam process config
    api_key = os.getenv("ROBOT_API_KEY")
    api_key_id = os.getenv("ROBOT_API_KEY_ID")
    if not api_key or not api_key_id:
        print("Error: ROBOT_API_KEY or ROBOT_API_KEY_ID not set in environment")
        return

    robot = await RobotClient.at_address(
        "inventorymonitorer-main.70yfjlr1vp.viam.cloud",
        DialOptions.with_api_key(api_key=api_key, api_key_id=api_key_id)
    )
    await robot.restart_module("local-module-1")
    print("Restarted local-module-1")
    await robot.close()

if __name__ == "__main__":
    asyncio.run(restart_module())