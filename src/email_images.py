import asyncio
import datetime
import tempfile
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Any, cast, ClassVar, Final, Mapping, Optional, Sequence
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
    MODEL: ClassVar[Model] = Model(
        ModelFamily("hunter", "sensor"), "image-emailer"
    )

    @classmethod
    def new(
        cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        sensor = cls(config)
        sensor.reconfigure(config, dependencies)
        return sensor

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        deps = []
        attributes = struct_to_dict(config.attributes)
        if "email" not in attributes:
            raise Exception("email is required")
        if "password" not in attributes:
            raise Exception("password is required")
        if "camera" not in attributes:
            raise Exception("camera is required")
        else:
            deps.append(attributes["camera"])
        return deps

    def __init__(self, config: ComponentConfig):
        super().__init__(config)
        self.email = ""
        self.password = ""
        self.frequency = 3600
        self.timeframe = [7, 19]
        self.camera = None
        self.camera_name = ""
        self.last_sent_time = None
        self.recipients = []
        self.save_dir = "/tmp"

    def reconfigure(
        self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ):
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.frequency = attributes.get("frequency", 3600)
        self.timeframe = attributes.get("timeframe", [7,19])
        self.camera_name = attributes["camera"]
        self.recipients = attributes.get("recipients",[])
        self.save_dir = attributes.get("save_dir", "/tmp")
        # Get crop parameters
        self.crop_top = attributes.get("crop_top", 0)
        self.crop_left = attributes.get("crop_left", 0)
        self.crop_width = attributes.get("crop_width", 0)
        self.crop_height = attributes.get("crop_height", 0)
        # Create the properly formatted ResourceName for the camera
        camera_resource_name = ResourceName(
            namespace="rdk", type="component", subtype="camera", name=self.camera_name
        )
        if camera_resource_name in dependencies:
            self.camera = dependencies[camera_resource_name]
            print(f"Successfully resolved camera: {self.camera_name}")
        else:
            self.camera = None
            print(f"Could not resolve camera: {self.camera_name}. Please check the camera name in your configuration.")
        self.last_sent_time = None
        return super().reconfigure(config, dependencies)

    async def get_readings(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional(float) = None,
        **kwargs
    ) -> Mapping[str, SensorReading]:
        if not self.camera:
            print("No camera available. Cannot capture image.")
            return {"error": "No camera available"}
        now = datetime.now()
        # Convert to EST by adding 3 hours
        est_now = now + datetime.timedelta(hours=3)
        current_time = est_now.hour
        start_time, end_time = self.timeframe
        if start_time <= current_time < end_time:
            if self.last_sent_time is None or (now - self.last_sent_time).total_seconds() >= self.frequency:
                try:
                    image = await self.camera.get_image()
                    image_data = image.data
                    # Load the image using PIL
                    img = Image.open(BytesIO(image_data))
                    # Get crop parameters
                    crop_top = self.crop_top
                    crop_left = self.crop_left
                    crop_width = self.crop_width or img.width - crop_left
                    crop_height = self.crop_height or img.height - crop_top
                    # Ensure that the crop dimensions are within the image bounds
                    crop_top = max(0, min(crop_top, img.height - 1))
                    crop_left = max(0, min(crop_left, img.width - 1))
                    crop_width = min(crop_width, img.width - crop_left)
                    crop_height = min(crop_height, img.height - crop_top)
                    # Crop the image
                    cropped_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))
                    # Save the cropped image to temporary file
                    image_filename = f"image_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                        cropped_img.save(tmp.name, format="JPEG")
                        image_path = tmp.name
                    # Save the image to the save directory if specified
                    if self.save_dir:
                        save_path = os.path.join(self.save_dir, image_filename)
                        cropped_img.save(save_path, format="JPEG")
                    # Send email
                    self.send_email("Automated Image Capture", f"Image captured at {now.strftime('%Y-%m-%d %H:%M:%S')}", image_path, image_filename)
                    # Clean up temporary file
                    os.unlink(image_path)
                    self.last_sent_time = now
                    return {"sending_email": True}
                except Exception as e:
                    error_msg = f"Error taking/sending image: {e}"
                    print(error_msg)
                    return {"error": error_msg}
        return {"sending_email": False}

    def send_email(self, subject, body, image_path, image_filename):
        msg = MIMEMultipart()
        msg["From"] = self.email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with open(image_path, "rb") as file:
            attachment = MIMEBase("application", "octet-stream")
            attachment.set_payload(file.read())
            encoders.encode_base64(attachment)
            attachment.add_header(
                "Content-Dis position", f"attachment; filename={image_filename}"
            )
            msg.attach(attachment)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(self.email, self.password)
            for recipient in self.recipients:
                msg["To"] = recipient
                smtp.send_message(msg)
                print(f"Email sent to {recipient} successfully!")

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())