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
import functools
import json
import fasteners
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("EmailImages")

class EmailImages(Sensor, EasyResource):
    MODEL: ClassVar[Model] = Model(ModelFamily("hunter", "sensor"), "image-emailer")

    @classmethod
    async def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        sensor = cls(config)
        logger.info(f"Created new EmailImages instance for {config.name} with PID {os.getpid()}")
        await sensor.reconfigure(config, dependencies)
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
        self.timeframe = [7, 20]  # 7 AM to 8 PM EST
        self.send_time = 20       # 8 PM EST
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/images"
        self.last_capture_time = None
        self.last_sent_date = None
        self.email_status = "not_sent"
        self.capture_loop_task = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        self.lock = asyncio.Lock()
        self.process_lock = fasteners.InterProcessLock(os.path.join(self.base_dir, "lockfile"))
        self.state_file = os.path.join(self.base_dir, "state.json")
        self._load_state()
        logger.info(f"Initialized with name: {self.name}, base_dir: {self.base_dir}, PID: {os.getpid()}")

    def _load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
                self.last_sent_date = state.get("last_sent_date")
                self.last_capture_time = (datetime.datetime.fromisoformat(state["last_capture_time"])
                                          if state.get("last_capture_time") else None)
            logger.info(f"Loaded state: last_sent_date={self.last_sent_date}, last_capture_time={self.last_capture_time}")
        else:
            logger.info(f"No state file at {self.state_file}, using defaults")

    def _save_state(self):
        state = {
            "last_sent_date": self.last_sent_date,
            "last_capture_time": self.last_capture_time.isoformat() if self.last_capture_time else None
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)
        logger.info(f"Saved state to {self.state_file}")

    def _get_last_capture_time(self, daily_dir):
        if not os.path.exists(daily_dir):
            logger.info(f"No daily directory exists at {daily_dir}")
            return None
        images = [f for f in os.listdir(daily_dir) if f.startswith("image_") and f.endswith("_EST.jpg")]
        if not images:
            logger.info(f"No valid images found in {daily_dir}")
            return None
        latest = max(images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        timestamp_str = latest.split('_')[1] + "_" + latest.split('_')[2].split('.')[0]
        try:
            last_time = datetime.datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            logger.info(f"Found latest image {latest}, last_capture_time={last_time}")
            return last_time
        except ValueError:
            logger.info(f"Invalid timestamp in {latest}")
            return None

    async def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.timeframe = attributes.get("timeframe", [7, 20])
        self.send_time = int(float(attributes.get("send_time", 20)))
        self.camera_name = attributes["camera"]
        self.recipients = attributes["recipients"]
        self.base_dir = attributes.get("save_dir", "/home/hunter.volkman/images")
        self.crop_top = int(float(attributes.get("crop_top", 0)))
        self.crop_left = int(float(attributes.get("crop_left", 0)))
        self.crop_width = int(float(attributes.get("crop_width", 0)))
        self.crop_height = int(float(attributes.get("crop_height", 0)))

        camera_resource_name = ResourceName(
            namespace="rdk", type="component", subtype="camera", name=self.camera_name
        )
        # Retry camera resolution up to 5 times with delay
        for attempt in range(5):
            self.camera = dependencies.get(camera_resource_name)
            if self.camera:
                logger.info(f"Successfully resolved camera: {self.camera_name} on attempt {attempt + 1}")
                break
            logger.warning(f"Could not resolve camera: {self.camera_name} on attempt {attempt + 1}, retrying in 2s")
            await asyncio.sleep(2)
        else:
            logger.error(f"Failed to resolve camera: {self.camera_name} after 5 attempts")

        today = datetime.datetime.now().strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today)
        if self.last_capture_time is None:
            self.last_capture_time = self._get_last_capture_time(daily_dir)
        self.email_status = "not_sent"
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)
        logger.info(f"Reconfigured {self.name} with base_dir: {self.base_dir}, last_capture_time: {self.last_capture_time}, PID: {os.getpid()}")

        if self.capture_loop_task:
            self.capture_loop_task.cancel()
            try:
                await asyncio.wait_for(self.capture_loop_task, timeout=5)
                logger.info("Previous capture_loop_task cancelled successfully")
            except asyncio.TimeoutError:
                logger.warning("Previous capture_loop_task did not cancel within 5 seconds")
        self.capture_loop_task = asyncio.create_task(self.capture_loop())

    async def capture_loop(self):
        with self.process_lock:
            logger.info(f"Process lock acquired, starting capture loop, PID: {os.getpid()}")
            while True:
                try:
                    now = datetime.datetime.now()
                    today_str = now.strftime("%Y%m%d")
                    tasks = asyncio.all_tasks()
                    logger.info(f"Active tasks: {len(tasks)} - {[task.get_name() for task in tasks]}")
                    async with self.lock:
                        start_time, end_time = [int(float(t)) for t in self.timeframe]
                        if now.hour in range(7, 20) and (self.last_capture_time is None or now.hour > self.last_capture_time.hour):
                            await self.capture_image(now)
                            self._save_state()
                        if now.hour == self.send_time and self.last_sent_date != today_str:
                            await self.send_report(now)
                            self.last_sent_date = today_str
                            self._save_state()
                    await asyncio.sleep(60 - now.second + 0.1)
                except asyncio.CancelledError:
                    logger.info("Capture loop cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Capture loop error: {str(e)}, retrying in 60s")
                    await asyncio.sleep(60)

    async def capture_image(self, now):
        if not self.camera:
            logger.error(f"No camera at {now}")
            return
        try:
            logger.info(f"Capturing image at {now}")
            image = await self.camera.get_image()
            img = Image.open(BytesIO(image.data))
            crop_width = self.crop_width or img.width - self.crop_left
            crop_height = self.crop_height or img.height - self.crop_top
            crop_top = max(0, min(self.crop_top, img.height - 1))
            crop_left = max(0, min(self.crop_left, img.width - 1))
            crop_width = min(crop_width, img.width - crop_left)
            crop_height = min(crop_height, img.height - crop_top)
            cropped_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))

            today_str = now.strftime('%Y%m%d')
            daily_dir = os.path.join(self.base_dir, today_str)
            os.makedirs(daily_dir, exist_ok=True)
            filename = f"image_{now.strftime('%Y%m%d_%H%M%S')}_EST.jpg"
            save_path = os.path.join(daily_dir, filename)
            cropped_img.save(save_path, "JPEG")
            self.last_capture_time = now
            logger.info(f"Saved image: {save_path}")
        except Exception as e:
            logger.error(f"Capture error at {now}: {str(e)}")

    async def send_report(self, now):
        today_str = now.strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today_str)
        if not os.path.exists(daily_dir):
            logger.info(f"No directory for {today_str}, skipping report")
            self.email_status = "no_images"
            return

        all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{today_str}") and f.endswith("_EST.jpg")]
        if not all_images:
            logger.info(f"No images for {today_str}, skipping report")
            self.email_status = "no_images"
            return

        images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        try:
            logger.info(f"Sending report with {len(images_to_send)} images at {now}")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                functools.partial(self._send_daily_report_sync, images_to_send, now, daily_dir)
            )
            self.email_status = "sent"
            logger.info(f"Sent report with {len(images_to_send)} images to {', '.join(self.recipients)}")
        except Exception as e:
            self.email_status = f"error: {str(e)}"
            logger.error(f"Email send error at {now}: {str(e)}")

    def _send_daily_report_sync(self, image_files, timestamp, daily_dir):
        msg = MIMEMultipart()
        msg["From"] = self.email
        msg["Subject"] = f"Daily Inventory Report - 389 5th Ave, New York, NY - {timestamp.strftime('%Y-%m-%d')}"
        body = f"Attached are {len(image_files)} inventory images from the store at 389 5th Ave, New York, NY captured on {timestamp.strftime('%Y-%m-%d')}, ordered from earliest to latest."
        msg.attach(MIMEText(body, "plain"))
        msg["To"] = ", ".join(self.recipients)

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
            smtp.send_message(msg)
            logger.info(f"Daily report sent to {msg['To']}")

    async def do_command(self, command: Mapping[str, Any], *, timeout: Optional[float] = None, **kwargs) -> Mapping[str, Any]:
        if command.get("command") == "send_email":
            day = command.get("day", datetime.datetime.now().strftime('%Y%m%d'))
            try:
                timestamp = datetime.datetime.strptime(day, '%Y%m%d')
                daily_dir = os.path.join(self.base_dir, day)
                if not os.path.exists(daily_dir):
                    logger.info(f"No directory for {day}")
                    return {"status": f"No images directory for {day}"}

                all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{day}") and f.endswith("_EST.jpg")]
                if not all_images:
                    logger.info(f"No images for {day}")
                    return {"status": f"No images found for {day}"}

                images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
                logger.info(f"Manual send for {day} with {len(images_to_send)} images")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    functools.partial(self._send_daily_report_sync, images_to_send, timestamp, daily_dir)
                )
                logger.info(f"Manual report sent with {len(images_to_send)} images to {', '.join(self.recipients)}")
                return {"status": f"Sent email with {len(images_to_send)} images for {day}"}
            except ValueError:
                return {"status": f"Invalid day format: {day}, use YYYYMMDD"}
            except Exception as e:
                return {"status": f"Error sending email: {str(e)}"}
        return {"status": "Unknown command"}

    async def get_readings(self, *, extra: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> Mapping[str, SensorReading]:
        now = datetime.datetime.now()
        logger.info(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}")
        if not self.camera:
            return {"error": "No camera available"}
        return {
            "status": "running",
            "last_capture_time": str(self.last_capture_time) if self.last_capture_time else "None",
            "email_status": self.email_status,
            "last_sent_date": self.last_sent_date if self.last_sent_date else "Never"
        }

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())