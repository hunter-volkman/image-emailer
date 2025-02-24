import asyncio
from viam.module.module import Module
from viam.components.sensor import Sensor
from src.email_images import EmailImages

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())