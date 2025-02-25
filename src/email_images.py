import asyncio
import datetime
import json
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
        required = ["email", "password", "camera", "recipients", "schedule"]
        for attr in required:
            if attr not in attributes:
                raise Exception(f"{attr} is required")
        for time in attributes["schedule"]:
            if not (0 <= time <= 2359 and time % 100 < 60):
                raise Exception(f"Schedule time {time} must be in HHMM format (0000-2359)")
        send_time = attributes.get("send_time", 2000)
        if not (0 <= send_time <= 2359 and send_time % 100 < 60):
            raise Exception(f"send_time {send_time} must be in HHMM format (0000-2359)")
        return [attributes["camera"]]

    def __init__(self, config: ComponentConfig):
        super().__init__(config.name)
        self.email = ""
        self.password = ""
        self.schedule = []  # List of HHMM times
        self.timeframe = [600, 2000]  # 6:00 AM to 8:00 PM EST in HHMM
        self.send_time = 2000         # 8:00 PM EST in HHMM
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/store_images"
        self.startup_dir = os.path.join(self.base_dir, "startup")
        self.state_file = os.path.join(self.base_dir, "state.json")
        self.last_capture_time = None
        self.last_send_time = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        self._load_state()
        print(f"Initialized {self.name} with base_dir: {self.base_dir}")

    def _load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
                if "last_capture_time" in state:
                    self.last_capture_time = datetime.datetime.strptime(state["last_capture_time"], "%Y-%m-%d %H:%M:%S")
                if "last_send_time" in state:
                    self.last_send_time = datetime.datetime.strptime(state["last_send_time"], "%Y-%m-%d %H:%M:%S")
            print(f"Loaded state: last_capture={self.last_capture_time}, last_send={self.last_send_time}")

    def _save_state(self):
        state = {
            "last_capture_time": self.last_capture_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_capture_time else None,
            "last_send_time": self.last_send_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_send_time else None
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)
        print(f"Saved state: {state}")

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.schedule = attributes["schedule"]
        self.timeframe = attributes.get("timeframe", [600, 2000])
        self.send_time = attributes.get("send_time", 2000)
        self.camera_name = attributes["camera"]
        self.recipients = attributes["recipients"]
        self.base_dir = attributes.get("save_dir", "/home/hunter.volkman/store_images")
        self.startup_dir = os.path.join(self.base_dir, "startup")
        self.state_file = os.path.join(self.base_dir, "state.json")
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

        for dir_path in [self.base_dir, self.startup_dir]:
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)
        print(f"Reconfigured {self.name} with schedule: {self.schedule}, send_time: {self.send_time}")

    async def get_readings(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, SensorReading]:
        now = datetime.datetime.now()  # EST
        current_hhmm = now.hour * 100 + now.minute
        print(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}, HHMM: {current_hhmm}")
        
        if not self.camera:
            print("No camera available.")
            return {"error": "No camera available"}

        start_time, end_time = self.timeframe
        print(f"Checking timeframe [{start_time}, {end_time}]")
        today = now.strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today)
        cropped_dir = os.path.join(daily_dir, "cropped")
        for dir_path in [daily_dir, cropped_dir]:
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)

        # Initial startup capture
        if self.last_capture_time is None:
            await self._capture_image(now, self.startup_dir, "startup")
            self._save_state()

        # Regular captures
        if start_time <= current_hhmm < end_time:
            current_time = now.hour * 100 + now.minute
            print(f"Current time: {current_time}, checking schedule {self.schedule}")
            if current_time in self.schedule:
                await self._capture_image(now, daily_dir, "hourly")
                self._save_state()

        # Daily report
        send_hour = self.send_time // 100
        send_minute = self.send_time % 100
        if now.hour == send_hour and now.minute == send_minute and (not self.last_send_time or (now - self.last_send_time).days >= 1):
            print(f"Send time {self.send_time} matched, preparing report for {today}")
            await self._send_report(today, daily_dir, cropped_dir, now)
            self.last_send_time = now
            self._save_state()
            return {"email_sent": True}

        return {"status": "running"}

    async def _capture_image(self, now: datetime.datetime, target_dir: str, prefix: str):
        try:
            print(f"Attempting to capture {prefix} image")
            image = await self.camera.get_image()
            print("Got image, processing")
            img = Image.open(BytesIO(image.data))
            filename = f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}_EST.jpg"
            save_path = os.path.join(target_dir, filename)
            img.save(save_path, format="JPEG")
            self.last_capture_time = now
            print(f"Saved {prefix} image: {save_path}")
        except Exception as e:
            print(f"Error capturing {prefix} image: {str(e)}")
            raise

    async def _send_report(self, today: str, daily_dir: str, cropped_dir: str, timestamp: datetime.datetime):
        images = [f for f in os.listdir(daily_dir) if f.startswith("hourly_")]
        if not images:
            print("No hourly images to send for today.")
            return

        print(f"Preparing report with {len(images)} hourly images")
        msg = MIMEMultipart()
        msg["From"] = self.email
        msg["Subject"] = f"Daily Shelf Report - {timestamp.strftime('%Y-%m-%d')}"
        body = f"Attached are {len(images)} shelf images captured on {timestamp.strftime('%Y-%m-%d')} EST."
        msg.attach(MIMEText(body, "plain"))

        for image_file in images:
            orig_path = os.path.join(daily_dir, image_file)
            cropped_file = f"cropped_{image_file}"
            cropped_path = os.path.join(cropped_dir, cropped_file)
            
            img = Image.open(orig_path)
            crop_width = self.crop_width or img.width - self.crop_left
            crop_height = self.crop_height or img.height - self.crop_top
            crop_top = max(0, min(self.crop_top, img.height - 1))
            crop_left = max(0, min(self.crop_left, img.width - 1))
            crop_width = min(crop_width, img.width - crop_left)
            crop_height = min(crop_height, img.height - crop_top)
            cropped_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))
            cropped_img.save(cropped_path, format="JPEG")
            print(f"Created cropped image: {cropped_path}")

            with open(cropped_path, "rb") as file:
                attachment = MIMEBase("application", "octet-stream")
                attachment.set_payload(file.read())
                encoders.encode_base64(attachment)
                attachment.add_header("Content-Disposition", f"attachment; filename={cropped_file}")
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