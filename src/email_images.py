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

# Constants for image suffixes to avoid magic strings
PRIMARY_SUFFIX = "_primary_EST.jpg"
SECONDARY_SUFFIX = "_secondary_EST.jpg"
STACKED_SUFFIX = "_stacked_EST.jpg"

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
        required = ["email", "password", "primary_camera", "recipients", "location"]
        for attr in required:
            if attr not in attributes:
                raise Exception(f"{attr} is required")
        # Validate capture_times_weekday if provided
        if "capture_times_weekday" in attributes:
            for time_str in attributes["capture_times_weekday"]:
                try:
                    datetime.datetime.strptime(time_str, "%H:%M")
                except ValueError:
                    raise Exception(f"Invalid capture_times_weekday entry '{time_str}': must be in 'HH:MM' format")
        # Validate capture_times_weekend if provided
        if "capture_times_weekend" in attributes:
            for time_str in attributes["capture_times_weekend"]:
                try:
                    datetime.datetime.strptime(time_str, "%H:%M")
                except ValueError:
                    raise Exception(f"Invalid capture_times_weekend entry '{time_str}': must be in 'HH:MM' format")
        # Validate send_time
        if "send_time" in attributes:
            try:
                datetime.datetime.strptime(attributes["send_time"], "%H:%M")
            except ValueError:
                raise Exception(f"Invalid send_time '{attributes['send_time']}': must be in 'HH:MM' format")
        # Validate stack_orientation if provided
        if "stack_orientation" in attributes:
            if attributes["stack_orientation"] not in ["vertical", "horizontal"]:
                raise Exception(f"Invalid stack_orientation '{attributes['stack_orientation']}': must be 'vertical' or 'horizontal'")
        # Return dependencies (primary camera is required, secondary is optional)
        dependencies = [attributes["primary_camera"]]
        if "secondary_camera" in attributes:
            dependencies.append(attributes["secondary_camera"])
        return dependencies

    def __init__(self, config: ComponentConfig):
        super().__init__(config.name)
        self.email = ""
        self.password = ""
        self.capture_times_weekday = []
        self.capture_times_weekend = []
        self.send_time = "20:00"
        self.primary_camera = None
        self.primary_camera_name = ""
        self.secondary_camera = None
        self.secondary_camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/images"
        # Unique directory per sensor
        self.sensor_dir = os.path.join(self.base_dir, self.name)
        self.last_capture_time = None
        self.last_sent_date = None
        self.last_sent_time = None
        self.report = "not_sent"
        self.capture_loop_task = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        self.make_gif = False
        self.location = ""
        self.stack_images = False  # Feature flag for stacking images
        self.stack_orientation = "vertical"  # Default to vertical stacking
        # Use sensor name to create unique state and lock files per sensor
        self.state_file = os.path.join(self.base_dir, f"state_{self.name}.json")
        self.lock_file = os.path.join(self.base_dir, f"lockfile_{self.name}")
        self._load_state()
        LOGGER.info(f"Initialized EmailImages with name: {self.name}, sensor_dir: {self.sensor_dir}, PID: {os.getpid()}, location: {self.location}")

    def _load_state(self):
        """Load persistent state from file."""
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
                self.last_sent_date = state.get("last_sent_date")
                self.last_sent_time = state.get("last_sent_time")
                self.last_capture_time = (
                    datetime.datetime.fromisoformat(state["last_capture_time"])
                    if state.get("last_capture_time")
                    else None
                )
            LOGGER.info(f"Loaded state from {self.state_file}: last_sent_date={self.last_sent_date}, last_sent_time={self.last_sent_time}, last_capture_time={self.last_capture_time}")
        else:
            LOGGER.info(f"No state file at {self.state_file}, starting fresh")

    def _save_state(self):
        """Save state to file for persistence across restarts."""
        state = {
            "last_sent_date": self.last_sent_date,
            "last_sent_time": self.last_sent_time,
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
        self.capture_times_weekday = attributes.get("capture_times_weekday", ["7:00", "7:15", "8:00", "11:00", "11:30"])
        self.capture_times_weekend = attributes.get("capture_times_weekend", ["8:00", "8:15", "9:00", "11:00", "11:30"])
        self.send_time = attributes.get("send_time", "20:00")
        self.primary_camera_name = attributes["primary_camera"]
        self.secondary_camera_name = attributes.get("secondary_camera", "")
        self.recipients = attributes["recipients"]
        self.base_dir = attributes.get("save_dir", "/home/hunter.volkman/images")
        # Update sensor-specific directory
        self.sensor_dir = os.path.join(self.base_dir, self.name)
        self.crop_top = int(float(attributes.get("crop_top", 0)))
        self.crop_left = int(float(attributes.get("crop_left", 0)))
        self.crop_width = int(float(attributes.get("crop_width", 0)))
        self.crop_height = int(float(attributes.get("crop_height", 0)))
        self.make_gif = bool(attributes.get("make_gif", False))
        self.location = attributes.get("location", "")
        self.stack_images = bool(attributes.get("stack_images", False))
        self.stack_orientation = attributes.get("stack_orientation", "vertical")

        # Update dependencies on reconfigure
        self._dependencies = dependencies
        LOGGER.info(f"Reconfigured {self.name} with sensor_dir: {self.sensor_dir}, last_capture_time: {self.last_capture_time}, capture_times_weekday: {self.capture_times_weekday}, capture_times_weekend: {self.capture_times_weekend}, make_gif: {self.make_gif}, location: {self.location}, stack_images: {self.stack_images}, stack_orientation: {self.stack_orientation}")

        if not os.path.exists(self.sensor_dir):
            os.makedirs(self.sensor_dir)

        if self.capture_loop_task:
            self.capture_loop_task.cancel()
        self.capture_loop_task = asyncio.create_task(self.run_scheduled_loop())

    def _get_capture_times_for_day(self, date: datetime.date) -> list[str]:
        """Return the appropriate capture times based on the day of the week."""
        # Monday (0) to Friday (4)
        if date.weekday() < 5:
            return self.capture_times_weekday
        # Saturday (5) and Sunday (6)
        else:
            return self.capture_times_weekend

    def _get_next_capture_time(self, now: datetime.datetime) -> datetime.datetime:
        """Calculate the next capture time based on current time and day-specific capture times."""
        today = now.date()
        tomorrow = today + datetime.timedelta(days=1)

        # Today’s capture times
        capture_times_today = self._get_capture_times_for_day(today)
        capture_datetimes_today = [
            datetime.datetime.combine(today, datetime.datetime.strptime(t, "%H:%M").time())
            for t in capture_times_today
        ]

        # Tomorrow’s capture times
        capture_times_tomorrow = self._get_capture_times_for_day(tomorrow)
        capture_datetimes_tomorrow = [
            datetime.datetime.combine(tomorrow, datetime.datetime.strptime(t, "%H:%M").time())
            for t in capture_times_tomorrow
        ]

        # Combine and find the next capture after now
        all_capture_datetimes = capture_datetimes_today + capture_datetimes_tomorrow
        future_captures = [dt for dt in all_capture_datetimes if dt > now]
        if future_captures:
            return min(future_captures)
        else:
            # Fallback: first capture time of the day after tomorrow (the rare case)
            day_after_tomorrow = tomorrow + datetime.timedelta(days=1)
            capture_times_next = self._get_capture_times_for_day(day_after_tomorrow)
            return datetime.datetime.combine(day_after_tomorrow, datetime.datetime.strptime(capture_times_next[0], "%H:%M").time())

    def _get_next_send_time(self, now: datetime.datetime) -> datetime.datetime:
        """Calculate the next send time based on current time and send_time."""
        today = now.date()
        send_time_dt = datetime.datetime.combine(today, datetime.datetime.strptime(self.send_time, "%H:%M").time())
        if now > send_time_dt:
            send_time_dt += datetime.timedelta(days=1)
        return send_time_dt

    async def run_scheduled_loop(self):
        """Run a scheduled loop that wakes up for specific capture times and send_time."""
        lock = fasteners.InterProcessLock(self.lock_file)
        if not lock.acquire(blocking=False):
            LOGGER.info(f"Another instance already running for {self.name} (PID {os.getpid()}), exiting")
            return
        try:
            LOGGER.info(f"Started scheduled loop for {self.name} with PID {os.getpid()}")
            while True:
                now = datetime.datetime.now()
                today_str = now.strftime("%Y%m%d")

                # Determine next capture time
                next_capture = self._get_next_capture_time(now)
                sleep_until_capture = (next_capture - now).total_seconds()

                # Determine next send time
                next_send = self._get_next_send_time(now)
                sleep_until_send = (next_send - now).total_seconds()

                # Sleep until the earliest event
                sleep_seconds = min(sleep_until_capture, sleep_until_send)
                LOGGER.info(f"Sleeping for {sleep_seconds:.0f} seconds until {min(next_capture, next_send)}")
                await asyncio.sleep(sleep_seconds)

                now = datetime.datetime.now()
                # Check if it's time to capture
                if now >= next_capture and (self.last_capture_time is None or now > self.last_capture_time):
                    primary_resource_name = ResourceName(
                        namespace="rdk", type="component", subtype="camera", name=self.primary_camera_name
                    )
                    self.primary_camera = self._dependencies.get(primary_resource_name)
                    if not self.primary_camera:
                        LOGGER.error(f"Primary camera {self.primary_camera_name} not available for {self.name}")
                    else:
                        # Set secondary camera if configured
                        if self.secondary_camera_name:
                            secondary_resource_name = ResourceName(
                                namespace="rdk", type="component", subtype="camera", name=self.secondary_camera_name
                            )
                            self.secondary_camera = self._dependencies.get(secondary_resource_name)
                            if not self.secondary_camera:
                                LOGGER.error(f"Secondary camera {self.secondary_camera_name} not available for {self.name}")
                        await self.capture_image(now)
                        self._save_state()
                    self.primary_camera = None
                    self.secondary_camera = None

                # Check if it's time to send the report
                send_time_today = datetime.datetime.strptime(self.send_time, "%H:%M").time()
                if (now.hour == send_time_today.hour and 
                    now.minute == send_time_today.minute and 
                    self.last_sent_date != today_str):
                    await self.send_report(now)
                    self.last_sent_date = today_str
                    self.last_sent_time = str(now)
                    self._save_state()

        except Exception as e:
            LOGGER.error(f"Scheduled loop failed for {self.name}: {str(e)}")
        finally:
            lock.release()
            LOGGER.info(f"Released lock for {self.name}, loop exiting (PID {os.getpid()})")

    async def capture_image(self, now):
        """Capture images from primary and secondary cameras with retry logic, optionally stacking them."""
        today_str = now.strftime('%Y%m%d')
        daily_dir = os.path.join(self.sensor_dir, today_str)
        os.makedirs(daily_dir, exist_ok=True)
        timestamp = now.strftime('%Y%m%d_%H%M%S')
        
        # Capture primary camera (ffmpeg)
        primary_path = None
        for attempt in range(3):
            try:
                LOGGER.info(f"Attempting primary capture for {self.name} at {now} (attempt {attempt + 1})")
                image = await self.primary_camera.get_image()
                img = Image.open(BytesIO(image.data))
                LOGGER.info(f"Captured primary image with raw dimensions: {img.width}x{img.height}")
                crop_width = self.crop_width or img.width - self.crop_left
                crop_height = self.crop_height or img.height - self.crop_top
                crop_top = max(0, min(self.crop_top, img.height - 1))
                crop_left = max(0, min(self.crop_left, img.width - 1))
                crop_width = min(crop_width, img.width - crop_left)
                crop_height = min(crop_height, img.height - crop_top)
                LOGGER.info(f"Applying crop to primary image: box=({crop_left}, {crop_top}, {crop_left + crop_width}, {crop_top + crop_height})")
                primary_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))
                LOGGER.info(f"Primary image cropped to: {primary_img.width}x{primary_img.height}")
                primary_filename = f"image_{timestamp}{PRIMARY_SUFFIX}"
                primary_path = os.path.join(daily_dir, primary_filename)
                primary_img.save(primary_path, "JPEG")
                LOGGER.info(f"Saved primary image for {self.name}: {primary_path}")
                break
            except Exception as e:
                LOGGER.warning(f"Primary capture failed for {self.name} (attempt {attempt + 1}): {str(e)}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    LOGGER.error(f"All primary capture attempts failed for {self.name} at {now}")
                    return

        # Capture secondary camera (langer_fill_view) if configured
        secondary_path = None
        if self.secondary_camera:
            for attempt in range(3):
                try:
                    LOGGER.info(f"Attempting secondary capture for {self.name} at {now} (attempt {attempt + 1})")
                    image = await self.secondary_camera.get_image()
                    img = Image.open(BytesIO(image.data))
                    LOGGER.info(f"Captured secondary image with raw dimensions: {img.width}x{img.height}")
                    sec_crop_width = min(self.crop_width or img.width - self.crop_left, img.width - self.crop_left)
                    sec_crop_height = min(self.crop_height or img.height - self.crop_top, img.height - self.crop_top)
                    sec_crop_top = max(0, min(self.crop_top, img.height - 1))
                    sec_crop_left = max(0, min(self.crop_left, img.width - 1))
                    sec_crop_width = min(sec_crop_width, img.width - sec_crop_left)
                    sec_crop_height = min(sec_crop_height, img.height - sec_crop_top)
                    LOGGER.info(f"Applying crop to secondary image: box=({sec_crop_left}, {sec_crop_top}, {sec_crop_left + sec_crop_width}, {sec_crop_top + sec_crop_height})")
                    secondary_img = img.crop((sec_crop_left, sec_crop_top, sec_crop_left + sec_crop_width, sec_crop_top + sec_crop_height))
                    LOGGER.info(f"Secondary image cropped to: {secondary_img.width}x{secondary_img.height}")
                    secondary_filename = f"image_{timestamp}{SECONDARY_SUFFIX}"
                    secondary_path = os.path.join(daily_dir, secondary_filename)
                    secondary_img.save(secondary_path, "JPEG")
                    LOGGER.info(f"Saved secondary image for {self.name}: {secondary_path}")
                    break
                except Exception as e:
                    LOGGER.warning(f"Secondary capture failed for {self.name} (attempt {attempt + 1}): {str(e)}")
                    if attempt < 2:
                        await asyncio.sleep(2)
                    else:
                        LOGGER.error(f"All secondary capture attempts failed for {self.name} at {now}")

        # Stack images if feature flag is enabled and both captures succeeded
        if self.stack_images and primary_path and secondary_path:
            try:
                primary_img = Image.open(primary_path)
                secondary_img = Image.open(secondary_path)
                LOGGER.info(f"Loaded images for stacking - primary: {primary_img.width}x{primary_img.height}, secondary: {secondary_img.width}x{secondary_img.height}")
                
                # Target size is primary's dimensions
                target_width = primary_img.width
                target_height = primary_img.height
                secondary_aspect = secondary_img.width / secondary_img.height
                LOGGER.info(f"Calculated secondary aspect ratio: {secondary_aspect:.2f}")

                # Resize secondary proportionally to fit within primary's dimensions
                if self.stack_orientation == "vertical":
                    new_width = target_width
                    new_height = int(new_width / secondary_aspect)
                    if new_height > target_height:
                        new_height = target_height
                        new_width = int(new_height * secondary_aspect)
                else:  # horizontal
                    new_height = target_height
                    new_width = int(new_height * secondary_aspect)
                    if new_width > target_width:
                        new_width = target_width
                        new_height = int(new_width / secondary_aspect)
                LOGGER.info(f"Resizing secondary image to fit {self.stack_orientation} stack: {new_width}x{new_height}")

                # Resize secondary image
                secondary_resized = secondary_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                LOGGER.info(f"Secondary image resized to: {secondary_resized.width}x{secondary_resized.height}")

                # Pad to match primary size
                secondary_padded = Image.new("RGB", (target_width, target_height), (0, 0, 0))
                paste_x = (target_width - new_width) // 2
                paste_y = (target_height - new_height) // 2
                LOGGER.info(f"Padding secondary image to {target_width}x{target_height}, pasting at ({paste_x}, {paste_y})")
                secondary_padded.paste(secondary_resized, (paste_x, paste_y))

                # Stack the images
                if self.stack_orientation == "vertical":
                    stacked_width = target_width
                    stacked_height = target_height * 2
                else:  # horizontal
                    stacked_width = target_width * 2
                    stacked_height = target_height
                LOGGER.info(f"Creating stacked image with dimensions: {stacked_width}x{stacked_height} ({self.stack_orientation})")
                stacked_img = Image.new("RGB", (stacked_width, stacked_height))
                stacked_img.paste(primary_img, (0, 0))
                stacked_img.paste(secondary_padded, (0, target_height) if self.stack_orientation == "vertical" else (target_width, 0))

                stacked_filename = f"image_{timestamp}{STACKED_SUFFIX}"
                stacked_path = os.path.join(daily_dir, stacked_filename)
                stacked_img.save(stacked_path, "JPEG")
                LOGGER.info(f"Saved stacked image for {self.name}: {stacked_path}")
            except Exception as e:
                LOGGER.error(f"Failed to stack images for {self.name}: {str(e)}")

        self.last_capture_time = now
        LOGGER.info(f"Completed capture for {self.name} at {now}")

    def annotate_image(self, image_path: str, font_path: Optional[str] = None, font_size: int = 20) -> Image.Image:
        """Annotate an image with its timestamp in the bottom-right corner."""
        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)

        # Extract timestamp from filename
        # e.g., image_20250304_090000_primary_EST.jpg
        filename = os.path.basename(image_path)
        try:
            parts = filename.split('_')
            if len(parts) >= 3:
                # e.g., "090000"
                time_str = parts[2]
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

    def _get_report_images(self, daily_dir: str, today_str: str) -> list[str]:
        """Filter images for the report and GIF based on stack_images setting."""
        all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{today_str}") and f.endswith("_EST.jpg")]
        if self.stack_images:
            return [f for f in all_images if f.endswith(STACKED_SUFFIX)]
        return [f for f in all_images if f.endswith(PRIMARY_SUFFIX)]

    def create_daily_gif(self, daily_dir: str, frame_duration: int = 1000, font_path: Optional[str] = None, font_size: int = 20) -> str:
        """Create an animated GIF from daily images, saved as 'daily.gif'."""
        today_str = os.path.basename(daily_dir)
        image_files = sorted(
            [os.path.join(daily_dir, f) for f in self._get_report_images(daily_dir, today_str)],
            key=lambda x: os.path.basename(x).split('_')[2]
        )
        if not image_files:
            LOGGER.warning(f"No suitable images found in {daily_dir} for GIF creation for {self.name}")
            raise ValueError("No images available for GIF")

        frames = []
        for image_path in image_files:
            frame = self.annotate_image(image_path, font_path, font_size)
            frames.append(frame.convert("P", palette=Image.ADAPTIVE))

        gif_path = os.path.join(daily_dir, "daily.gif")
        frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=frame_duration, loop=0)
        LOGGER.info(f"Created daily GIF for {self.name} at {gif_path} with {len(frames)} frames")
        return gif_path

    async def send_report(self, now):
        """Send a daily report with all captured images."""
        today_str = now.strftime('%Y%m%d')
        # Use sensor-specific directory
        daily_dir = os.path.join(self.sensor_dir, today_str)
        if not os.path.exists(daily_dir):
            LOGGER.info(f"No directory for {today_str} for {self.name}, skipping report")
            self.report = "no_images"
            return

        images_to_send = self._get_report_images(daily_dir, today_str)
        if not images_to_send:
            LOGGER.info(f"No suitable images for {today_str} for {self.name}, skipping report")
            self.report = "no_images"
            return

        images_to_send = sorted(images_to_send, key=lambda x: x.split('_')[1] + x.split('_')[2].split('_')[0])
        try:
            LOGGER.info(f"Sending report for {self.name} with {len(images_to_send)} images at {now}")
            await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(self._send_daily_report_sync, images_to_send, now, daily_dir)
            )
            self.report = "sent"
            self.last_sent_date = today_str
            self.last_sent_time = str(now)
            self._save_state()
            LOGGER.info(f"Sent report for {self.name} with {len(images_to_send)} images to {', '.join(self.recipients)}")
        except Exception as e:
            self.report = f"error: {str(e)}"
            LOGGER.error(f"Email send error for {self.name} at {now}: {str(e)}")

    def _send_daily_report_sync(self, image_files, timestamp, daily_dir):
        """Send the daily email report, optionally including a GIF if make_gif is enabled."""
        msg = MIMEMultipart("mixed")
        msg["From"] = self.email
        msg["Subject"] = f"Daily Report - {self.location} - {timestamp.strftime('%Y-%m-%d')}"
        msg["To"] = ", ".join(self.recipients)

        # Create GIF if enabled
        gif_path = None
        if self.make_gif:
            try:
                gif_path = self.create_daily_gif(daily_dir, frame_duration=1000, font_path=None, font_size=20)
            except Exception as e:
                LOGGER.error(f"Failed to create GIF for {self.name}: {str(e)}")

        # Build HTML body with optional GIF
        related = MIMEMultipart("related")
        html_body = f"""
        <html>
          <body>
            <p>Attached are {len(image_files)} images from {self.location} captured on {timestamp.strftime('%Y-%m-%d')}, ordered from earliest to latest.</p>
        """
        if gif_path:
            html_body += '<p>Daily GIF:</p><img src="cid:dailygif">'
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

        # Annotate and attach individual images
        for image_file in image_files:
            image_path = os.path.join(daily_dir, image_file)
            try:
                # Annotate the image
                annotated_img = self.annotate_image(image_path, font_path=None, font_size=20)
                
                # Create a temporary file for the annotated image
                temp_path = image_path.replace(".jpg", "_annotated.jpg")
                annotated_img.save(temp_path, "JPEG")
                
                # Attach the annotated image
                with open(temp_path, "rb") as file:
                    attachment = MIMEBase("application", "octet-stream")
                    attachment.set_payload(file.read())
                    encoders.encode_base64(attachment)
                    attachment.add_header("Content-Disposition", f"attachment; filename={os.path.basename(temp_path)}")
                    msg.attach(attachment)
                
                # Clean up the temporary file after attaching
                os.remove(temp_path)
            except Exception as e:
                LOGGER.warning(f"Failed to annotate or attach {image_file} for {self.name}: {str(e)}")
                # Fallback: attach the original image if annotation fails
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
            LOGGER.info(f"Daily report sent for {self.name} to {msg['To']}{' with GIF' if gif_path else ''}")

    async def do_command(self, command: Mapping[str, Any], *, timeout: Optional[float] = None, **kwargs) -> Mapping[str, Any]:
        if command.get("command") == "send_email":
            day = command.get("day", datetime.datetime.now().strftime('%Y%m%d'))
            try:
                timestamp = datetime.datetime.strptime(day, '%Y%m%d')
                # Use sensor-specific directory
                daily_dir = os.path.join(self.sensor_dir, day)
                if not os.path.exists(daily_dir):
                    LOGGER.info(f"No directory for {day} for {self.name}")
                    return {"status": f"No images directory for {day}"}

                images_to_send = self._get_report_images(daily_dir, day)
                if not images_to_send:
                    LOGGER.info(f"No suitable images for {day} for {self.name}")
                    return {"status": f"No images found for {day}"}

                images_to_send = sorted(images_to_send, key=lambda x: x.split('_')[1] + x.split('_')[2].split('_')[0])
                LOGGER.info(f"Manual send for {self.name} for {day} with {len(images_to_send)} images")
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(self._send_daily_report_sync, images_to_send, timestamp, daily_dir)
                )
                self.report = "sent"
                self.last_sent_date = day
                self.last_sent_time = str(timestamp)
                self._save_state()
                LOGGER.info(f"Manual report sent for {self.name} with {len(images_to_send)} images to {', '.join(self.recipients)}")
                return {"status": f"Sent email with {len(images_to_send)} images for {day}"}
            except ValueError:
                return {"status": f"Invalid day format: {day}, use YYYYMMDD"}
            except Exception as e:
                return {"status": f"Error sending email: {str(e)}"}
        
        elif command.get("command") == "create_gif":
            day = command.get("day", datetime.datetime.now().strftime('%Y%m%d'))
            try:
                datetime.datetime.strptime(day, '%Y%m%d')
                # Use sensor-specific directory
                daily_dir = os.path.join(self.sensor_dir, day)  
                if not os.path.exists(daily_dir):
                    LOGGER.info(f"No directory for {day} for {self.name}")
                    return {"status": f"No images directory for {day}"}

                images_to_send = self._get_report_images(daily_dir, day)
                if not images_to_send:
                    LOGGER.info(f"No suitable images for {day} for {self.name}")
                    return {"status": f"No images found for {day}"}

                LOGGER.info(f"Creating GIF for {self.name} for {day} with {len(images_to_send)} images")
                gif_path = await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(self.create_daily_gif, daily_dir)
                )
                return {"status": f"Created GIF for {day} at {gif_path}"}
            except ValueError:
                return {"status": f"Invalid day format: {day}, use YYYYMMDD"}
            except Exception as e:
                return {"status": f"Error creating GIF: {str(e)}"}

        return {"status": "Unknown command"}

    async def get_readings(self, *, extra: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> Mapping[str, SensorReading]:
        """Return the current state of the sensor, including scheduling details for debugging."""
        now = datetime.datetime.now()
        LOGGER.info(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}")
        next_send_time = self._get_next_send_time(now)
        return {
            "status": "running",
            "last_capture_time": str(self.last_capture_time) if self.last_capture_time else "none",
            "report": self.report,
            "last_sent_date": self.last_sent_date if self.last_sent_date else "never",
            "last_sent_time": str(datetime.datetime.fromisoformat(self.last_sent_time)) if self.last_sent_time and self.last_sent_time != "never" else "never",
            "pid": os.getpid(),
            "gif": self.make_gif,
            "location": self.location,
            "next_capture_time": str(self._get_next_capture_time(now)),
            "next_send_date": next_send_time.strftime("%Y%m%d"),
            "next_send_time": str(next_send_time),
            "capture_times_weekday": self.capture_times_weekday,
            "capture_times_weekend": self.capture_times_weekend,
            "state": self.state_file,
            "lock": self.lock_file,
            "stack_images": self.stack_images,
            "stack_orientation": self.stack_orientation
        }

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())