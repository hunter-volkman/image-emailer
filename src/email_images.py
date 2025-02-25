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
        self.timeframe = [7, 19]
        self.send_time = 19
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/images"
        self.last_capture_time = None
        self.sent_this_hour = False
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        print(f"Initialized EmailImages with name: {self.name}, base_dir: {self.base_dir}")

    def _get_last_capture_time(self, daily_dir):
        """Determine last capture time from files in daily_dir."""
        if not os.path.exists(daily_dir):
            print(f"No daily directory exists at {daily_dir}, last_capture_time remains None")
            return None
        images = [f for f in os.listdir(daily_dir) if f.startswith("image_")]
        if not images:
            print(f"No images found in {daily_dir}, last_capture_time remains None")
            return None
        latest = max(images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        timestamp_str = latest.split('_')[1] + "_" + latest.split('_')[2].split('.')[0]
        last_time = datetime.datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
        print(f"Found latest image {latest}, setting last_capture_time to {last_time}")
        return last_time

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.timeframe = attributes.get("timeframe", [7, 19])
        self.send_time = attributes.get("send_time", 19)
        self.camera_name = attributes["camera"]
        self.recipients = attributes["recipients"]
        self.base_dir = attributes.get("save_dir", "/home/hunter.volkman/images")
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
        
        today = datetime.datetime.now().strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today)
        self.last_capture_time = self._get_last_capture_time(daily_dir)
        self.sent_this_hour = False
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)
        print(f"Reconfigured {self.name} with base_dir: {self.base_dir}, last_capture_time: {self.last_capture_time}")

    async def get_readings(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, SensorReading]:
        # Local time is EST
        now = datetime.datetime.now()  
        current_hour = now.hour
        print(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}, hour: {current_hour}")
        
        if not self.camera:
            print("No camera available.")
            return {"error": "No camera available"}

        start_time, end_time = self.timeframe
        print(f"Checking timeframe [{start_time}, {end_time}]")
        today = now.strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today)
        if not os.path.exists(daily_dir):
            os.makedirs(daily_dir)

        # Determine next capture hour
        if self.last_capture_time:
            last_hour = self.last_capture_time.hour
            next_hour = last_hour + 1 if last_hour < end_time - 1 else start_time
            print(f"Last capture time: {self.last_capture_time}, last_hour: {last_hour}, next_hour: {next_hour}")
        else:
            # Start at first hour if no prior capture
            next_hour = start_time  
            print(f"No last capture time, setting next_hour to start_time: {next_hour}")

        # Capture if it's time for the next hour
        if start_time <= current_hour < end_time and current_hour >= next_hour:
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
                save_path = os.path.join(daily_dir, filename)
                cropped_img.save(save_path, format="JPEG")
                self.last_capture_time = now
                print(f"Saved image: {save_path} for hour {current_hour}, updated last_capture_time to {self.last_capture_time}")
            except Exception as e:
                print(f"Error capturing image: {str(e)}")
                return {"error": str(e)}

        if current_hour == self.send_time and not self.sent_this_hour:
            print(f"Send time {self.send_time} matched, preparing report for {today}")
            try:
                all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{today}")]
                images_by_hour = {}
                for img in all_images:
                    # Extract HH from YYYYMMDD_HHMMSS
                    hour = int(img.split('_')[1][8:10])
                    if start_time <= hour < end_time:
                        # Latest per hour
                        images_by_hour[hour] = img
                
                images_to_send = list(images_by_hour.values())
                if images_to_send:
                    self.send_daily_report(images_to_send, now, daily_dir)
                    self.sent_this_hour = True
                    print(f"Sent report with {len(images_to_send)} images; originals preserved.")
                    return {"email_sent": True}
                else:
                    print("No images to send for today within timeframe.")
            except Exception as e:
                print(f"Error sending email: {str(e)}")
                return {"error": str(e)}
        elif current_hour != self.send_time:
            self.sent_this_hour = False

        return {"status": "running"}

    def send_daily_report(self, image_files, timestamp, daily_dir):
        msg = MIMEMultipart()
        msg["From"] = self.email
        msg["Subject"] = f"Daily Shelf Report - {timestamp.strftime('%Y-%m-%d')}"
        body = f"Attached are {len(image_files)} shelf images captured on {timestamp.strftime('%Y-%m-%d')} EST."
        msg.attach(MIMEText(body, "plain"))

        for image_file in image_files:
            image_path = os.path.join(daily_dir, image_file)
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