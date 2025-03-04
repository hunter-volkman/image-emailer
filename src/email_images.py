import asyncio
import datetime
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
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
from viam.logging import getLogger
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import functools
import json
import fasteners

LOGGER = getLogger(__name__)

class EmailImages(Sensor, EasyResource):
    MODEL: ClassVar[Model] = Model(ModelFamily("hunter", "sensor"), "image-emailer")

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        sensor = cls(config)
        LOGGER.info(f"Created new EmailImages instance for {config.name} with PID {os.getpid()}")
        # Store dependencies for later use
        sensor._dependencies = dependencies
        sensor.reconfigure(config, dependencies)
        return sensor

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attributes = struct_to_dict(config.attributes)
        required = ["email", "password", "camera", "recipients", "location"]  # Added location as required
        for attr in required:
            if attr not in attributes:
                raise Exception(f"{attr} is required")
        return [attributes["camera"]]

    def __init__(self, config: ComponentConfig):
        super().__init__(config.name)
        self.email = ""
        self.password = ""
        self.timeframe = [7, 20]
        self.send_time = 20
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/images"
        self.last_capture_time = None
        self.last_sent_date = None
        self.report = "not_sent"
        self.capture_loop_task = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        self.make_gif = False
        self.location = ""
        self.state_file = os.path.join(self.base_dir, "state.json")
        self.lock_file = os.path.join(self.base_dir, "lockfile")
        self._load_state()
        LOGGER.info(f"Initialized EmailImages with name: {self.name}, base_dir: {self.base_dir}, PID: {os.getpid()}, location: {self.location}")

    def _load_state(self):
        """Load persistent state from file."""
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
                self.last_sent_date = state.get("last_sent_date")
                self.last_capture_time = (
                    datetime.datetime.fromisoformat(state["last_capture_time"])
                    if state.get("last_capture_time")
                    else None
                )
            LOGGER.info(f"Loaded state: last_sent_date={self.last_sent_date}, last_capture_time={self.last_capture_time}")
        else:
            LOGGER.info(f"No state file at {self.state_file}, starting fresh")

    def _save_state(self):
        """Save state to file for persistence across restarts."""
        state = {
            "last_sent_date": self.last_sent_date,
            "last_capture_time": self.last_capture_time.isoformat() if self.last_capture_time else None
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)
        LOGGER.info(f"Saved state to {self.state_file}")

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        """Configure the module and start the scheduled loop."""
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
        self.make_gif = bool(attributes.get("make_gif", False))
        self.location = attributes.get("location", "")

        # Update dependencies on reconfigure
        self._dependencies = dependencies
        LOGGER.info(f"Reconfigured {self.name} with base_dir: {self.base_dir}, last_capture_time: {self.last_capture_time}, make_gif: {self.make_gif}, location: {self.location}")

        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

        if self.capture_loop_task:
            self.capture_loop_task.cancel()
        self.capture_loop_task = asyncio.create_task(self.run_scheduled_loop())

    async def run_scheduled_loop(self):
        """Run a scheduled loop waking up at the start of each hour."""
        lock = fasteners.InterProcessLock(self.lock_file)
        if not lock.acquire(blocking=False):
            LOGGER.info(f"Another instance already running (PID {os.getpid()}), exiting")
            return
        try:
            LOGGER.info(f"Started scheduled loop with PID {os.getpid()}")
            while True:
                now = datetime.datetime.now()
                next_hour = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                sleep_seconds = (next_hour - now).total_seconds()
                LOGGER.info(f"Sleeping for {sleep_seconds:.0f} seconds until {next_hour}")
                await asyncio.sleep(sleep_seconds)

                now = datetime.datetime.now()
                today_str = now.strftime("%Y%m%d")
                start_time, end_time = self.timeframe

                # Capture if within timeframe and now qualifies new capture time
                if start_time <= now.hour < end_time and (self.last_capture_time is None or now > self.last_capture_time):
                    camera_resource_name = ResourceName(
                        namespace="rdk", type="component", subtype="camera", name=self.camera_name
                    )
                    self.camera = self._dependencies.get(camera_resource_name)
                    if not self.camera:
                        LOGGER.error(f"Camera {self.camera_name} not available")
                    else:
                        await self.capture_image(now)
                        self._save_state()
                    # Reset to avoid holding connection
                    self.camera = None

                # Send email if it's send_time and not sent today
                if now.hour == self.send_time and self.last_sent_date != today_str:
                    await self.send_report(now)
                    self.last_sent_date = today_str
                    self._save_state()
        except Exception as e:
            LOGGER.error(f"Scheduled loop failed: {str(e)}")
        finally:
            lock.release()
            LOGGER.info(f"Released lock, loop exiting (PID {os.getpid()})")

    async def capture_image(self, now):
        """Capture an image with retry logic for flaky connections."""
        for attempt in range(3):
            try:
                LOGGER.info(f"Attempting capture at {now} (attempt {attempt + 1})")
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
                LOGGER.info(f"Saved image: {save_path}")
                break
            except Exception as e:
                LOGGER.warning(f"Capture failed (attempt {attempt + 1}): {str(e)}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    LOGGER.error(f"All capture attempts failed at {now}")

    def annotate_image(self, image_path: str, font_path: Optional[str] = None, font_size: int = 20) -> Image.Image:
        """Annotate an image with its timestamp in the bottom-right corner."""
        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)

        # Extract timestamp from filename (e.g., image_20250304_090000_EST.jpg)
        filename = os.path.basename(image_path)
        try:
            parts = filename.split('_')
            if len(parts) >= 3:
                time_str = parts[2]  # e.g., "090000"
                formatted_time = f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]} EST"
            else:
                formatted_time = "unknown"
        except Exception:
            formatted_time = "unknown"

        # Load font (default to Arial if specified font fails)
        try:
            font = ImageFont.truetype(font_path if font_path else "arial.ttf", font_size)
        except IOError:
            font = ImageFont.load_default()
            LOGGER.warning(f"Font {font_path or 'arial.ttf'} not found, using default")

        # Position text in bottom-right with padding
        text_bbox = draw.textbbox((0, 0), formatted_time, font=font)
        text_width, text_height = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]
        x = img.width - text_width - 10
        y = img.height - text_height - 10

        # Semi-transparent black rectangle for readability
        draw.rectangle([x-5, y-5, x+text_width+5, y+text_height+5], fill=(0, 0, 0, 128))
        draw.text((x, y), formatted_time, fill="white", font=font)

        return img

    def create_daily_gif(self, daily_dir: str, frame_duration: int = 100, font_path: Optional[str] = None, font_size: int = 20) -> str:
        """Create an animated GIF from daily images, saved as 'daily.gif'."""
        image_files = sorted(
            [os.path.join(daily_dir, f) for f in os.listdir(daily_dir) if f.startswith("image_") and f.endswith("_EST.jpg")],
            key=lambda x: os.path.basename(x).split('_')[2]
        )
        if not image_files:
            LOGGER.warning(f"No images found in {daily_dir} for GIF creation")
            raise ValueError("No images available for GIF")

        frames = []
        for image_path in image_files:
            frame = self.annotate_image(image_path, font_path, font_size)
            frames.append(frame.convert("P", palette=Image.ADAPTIVE))

        gif_path = os.path.join(daily_dir, "daily.gif")
        frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=frame_duration, loop=0)
        LOGGER.info(f"Created daily GIF at {gif_path} with {len(frames)} frames")
        return gif_path

    async def send_report(self, now):
        """Send a daily report with all captured images."""
        today_str = now.strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today_str)
        if not os.path.exists(daily_dir):
            LOGGER.info(f"No directory for {today_str}, skipping report")
            self.report = "no_images"
            return

        all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{today_str}") and f.endswith("_EST.jpg")]
        if not all_images:
            LOGGER.info(f"No images for {today_str}, skipping report")
            self.report = "no_images"
            return

        images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        try:
            LOGGER.info(f"Sending report with {len(images_to_send)} images at {now}")
            await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(self._send_daily_report_sync, images_to_send, now, daily_dir)
            )
            self.report = "sent"
            LOGGER.info(f"Sent report with {len(images_to_send)} images to {', '.join(self.recipients)}")
        except Exception as e:
            self.report = f"error: {str(e)}"
            LOGGER.error(f"Email send error at {now}: {str(e)}")

    def _send_daily_report_sync(self, image_files, timestamp, daily_dir):
        """Send the daily email report, optionally including a GIF if make_gif is enabled."""
        msg = MIMEMultipart("mixed")
        msg["From"] = self.email
        msg["Subject"] = f"Daily Report - {self.location} - {timestamp.strftime('%Y-%m-%d')}"  # Updated subject
        msg["To"] = ", ".join(self.recipients)

        # Create GIF if enabled
        gif_path = None
        if self.make_gif:
            try:
                gif_path = self.create_daily_gif(daily_dir, frame_duration=100, font_path=None, font_size=20)
            except Exception as e:
                LOGGER.error(f"Failed to create GIF: {str(e)}")

        # Build HTML body with optional GIF
        related = MIMEMultipart("related")
        html_body = f"""
        <html>
          <body>
            <p>Attached are {len(image_files)} images from {self.location} captured on {timestamp.strftime('%Y-%m-%d')}, ordered from earliest to latest.</p>
        """
        if gif_path:
            html_body += '<p>Daily Summary GIF:</p><img src="cid:dailygif">'
        html_body += "</body></html>"
        related.attach(MIMEText(html_body, "html"))
        msg.attach(related)

        # Attach GIF inline if created
        if gif_path and os.path.exists(gif_path):
            with open(gif_path, "rb") as gif_file:
                gif_part = MIMEImage(gif_file.read(), _subtype="gif")
                gif_part.add_header("Content-ID", "<dailygif>")
                gif_part.add_header("Content-Disposition", "inline", filename="daily.gif")
                msg.attach(gif_part)

        # Attach individual images
        for image_file in image_files:
            image_path = os.path.join(daily_dir, image_file)
            with open(image_path, "rb") as file:
                attachment = MIMEBase("application", "octet-stream")
                attachment.set_payload(file.read())
                encoders.encode_base64(attachment)
                attachment.add_header("Content-Disposition", f"attachment; filename={image_file}")
                msg.attach(attachment)

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(self.email, self.password)
            smtp.send_message(msg)
            LOGGER.info(f"Daily report sent to {msg['To']}{' with GIF' if gif_path else ''}")

    async def do_command(self, command: Mapping[str, Any], *, timeout: Optional[float] = None, **kwargs) -> Mapping[str, Any]:
        if command.get("command") == "send_email":
            day = command.get("day", datetime.datetime.now().strftime('%Y%m%d'))
            try:
                timestamp = datetime.datetime.strptime(day, '%Y%m%d')
                daily_dir = os.path.join(self.base_dir, day)
                if not os.path.exists(daily_dir):
                    LOGGER.info(f"No directory for {day}")
                    return {"status": f"No images directory for {day}"}

                all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{day}") and f.endswith("_EST.jpg")]
                if not all_images:
                    LOGGER.info(f"No images for {day}")
                    return {"status": f"No images found for {day}"}

                images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
                LOGGER.info(f"Manual send for {day} with {len(images_to_send)} images")
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(self._send_daily_report_sync, images_to_send, timestamp, daily_dir)
                )
                LOGGER.info(f"Manual report sent with {len(images_to_send)} images to {', '.join(self.recipients)}")
                return {"status": f"Sent email with {len(images_to_send)} images for {day}"}
            except ValueError:
                return {"status": f"Invalid day format: {day}, use YYYYMMDD"}
            except Exception as e:
                return {"status": f"Error sending email: {str(e)}"}
        return {"status": "Unknown command"}

    async def get_readings(self, *, extra: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> Mapping[str, SensorReading]:
        now = datetime.datetime.now()
        LOGGER.info(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}")
        return {
            "status": "running",
            "last_capture_time": str(self.last_capture_time) if self.last_capture_time else "none",
            "report": self.report,
            "last_sent_date": self.last_sent_date if self.last_sent_date else "never",
            "pid": os.getpid(),
            "gif": self.make_gif,
            "location": self.location
        }

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())