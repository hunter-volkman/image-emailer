import asyncio
import datetime
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Any, ClassVar, Mapping, Optional, Sequence
from typing_extensions import Self
from viam.components.camera import Camera
from viam.components.sensor import Sensor
from viam.module.module import Module
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.utils import SensorReading, struct_to_dict
from PIL import Image
from io import BytesIO

class EmailImages(Sensor, EasyResource):
    MODEL: ClassVar[Model] = Model(ModelFamily("hunter", "sensor"), "image-emailer")

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        sensor = cls(config)
        print(f"Created new EmailImages instance for {config.name}")
        sensor.reconfigure(config, dependencies)
        return sensor

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attributes = struct_to_dict(config.attributes)
        required = ["email", "password", "camera", "recipients"]
        for attr in required:
            if attr not in attributes:
                raise Exception(f"{attr} is required")
        return [attributes["camera"]]

    def __init__(self, config: ComponentConfig):
        super().__init__(config.name)
        self.email = ""
        self.password = ""
        self.frequency = 3600  # 1 hour for tomorrow
        self.timeframe = [7, 19]  # 7 AM to 7 PM EST
        self.report_time = 19     # 7 PM EST
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.save_dir = "/home/hunter.volkman/images"  # Renamed from store_images
        self.last_capture_time = None
        self.last_report_time = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        print(f"Initialized EmailImages with name: {self.name}, save_dir: {self.save_dir}")

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.frequency = attributes.get("frequency", 3600)
        self.timeframe = attributes.get("timeframe", [7, 19])
        self.report_time = attributes.get("report_time", 19)
        self.camera_name = attributes["camera"]
        self.recipients = attributes["recipients"]
        self.save_dir = attributes.get("save_dir", "/home/hunter.volkman/images")  # Renamed
        self.crop_top = attributes.get("crop_top", 0)
        self.crop_left = attributes.get("crop_left", 0)
        self.crop_width = attributes.get("crop_width", 0)
        self.crop_height = attributes.get("crop_height", 0)
        
        camera_resource_name = ResourceName(
            namespace="rdk", type="component", subtype="camera", name=self.camera_name
        )
        self.camera = dependencies.get(camera_resource_name)
        if not self.camera:
            print(f"Could not resolve camera: {self.camera_name}. Check configuration.")
        else:
            print(f"Successfully resolved camera: {self.camera_name}")
        
        self.last_capture_time = None
        self.last_report_time = None
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        print(f"Reconfigured {self.name} with save_dir: {self.save_dir}, frequency: {self.frequency}s")

    async def get_readings(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, SensorReading]:
        now = datetime.datetime.now()  # Local time is EST
        current_hour = now.hour
        print(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}, hour: {current_hour}")
        
        if not self.camera:
            print("No camera available.")
            return {"error": "No camera available"}

        start_time, end_time = self.timeframe
        print(f"Checking timeframe [{start_time}, {end_time}]")

        if start_time <= current_hour < end_time:
            time_since_last = (now - self.last_capture_time).total_seconds() if self.last_capture_time else float('inf')
            print(f"Time since last capture: {time_since_last:.2f}s, frequency: {self.frequency}s")
            if not self.last_capture_time or time_since_last >= self.frequency:
                try:
                    print("Attempting to get image from camera")
                    image = await self.camera.get_image()
                    print("Got image, processing")
                    img = Image.open(BytesIO(image.data))
                    crop_width = self.crop_width or img.width - self.crop_left
                    crop_height = self.crop_height or img.height - self.crop_top
                    crop_top = max(0, min(self.crop_top, img.height - 1))
                    crop_left = max(0, min(self.crop_left, img.width - 1))
                    crop_width = min(crop_width, img.width - crop_left)
                    crop_height = min(crop_height, img.height - crop_top)
                    cropped_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))
                    
                    filename = f"image_{now.strftime('%Y%m%d_%H%M%S')}_EST.jpg"
                    save_path = os.path.join(self.save_dir, filename)
                    cropped_img.save(save_path, format="JPEG")
                    self.last_capture_time = now
                    print(f"Saved image: {save_path}")
                except Exception as e:
                    print(f"Error capturing image: {str(e)}")
                    return {"error": str(e)}

        if current_hour == self.report_time:
            today = now.strftime('%Y%m%d')
            print(f"Report time {self.report_time} matched, preparing report for {today}")
            if not self.last_report_time or (now - self.last_report_time).days >= 1:
                try:
                    images = [f for f in os.listdir(self.save_dir) if f.startswith(f"image_{today}")]
                    if images:
                        self.send_daily_report(images, now)
                        self.last_report_time = now
                        for img in images:
                            os.remove(os.path.join(self.save_dir, img))
                        print(f"Cleaned up {len(images)} images after sending.")
                        return {"email_sent": True}
                    else:
                        print("No images to send for today.")
                except Exception as e:
                    print(f"Error sending email: {str(e)}")
                    return {"error": str(e)}

        return {"status": "running"}

    def send_daily_report(self, image_files, timestamp):
        msg = MIMEMultipart()
        msg["From"] = self.email
        msg["Subject"] = f"Daily Shelf Report - {timestamp.strftime('%Y-%m-%d')}"
        body = f"Attached are {len(image_files)} shelf images captured on {timestamp.strftime('%Y-%m-%d')} EST."
        msg.attach(MIMEText(body, "plain"))

        for image_file in image_files:
            image_path = os.path.join(self.save_dir, image_file)
            with open(image_path, "rb") as file:
                attachment = MIMEBase("application", "octet-stream")
                attachment.set_payload(file.read())
                encoders.encode_base64(attachment)
                attachment.add_header(
                    "Content-Disposition", f"attachment; filename={image_file}"
                )
                msg.attach(attachment)

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(self.email, self.password)
            for recipient in self.recipients:
                msg["To"] = recipient
                smtp.send_message(msg)
                print(f"Daily report sent to {recipient}")

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())