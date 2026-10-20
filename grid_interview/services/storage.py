"""Recording storage behind one interface.

Local Private (current default, the server's EBS volume): files live OUTSIDE public/ and
private/files, so neither nginx nor Frappe's /private/files route can serve them; playback goes
only through grid_interview.api.recording.stream after a permission check.
S3 (later): private bucket, server-side encryption, short-lived presigned URLs.
Switching backend = change Grid Interview Settings › Storage Backend (+ copy existing files).
"""

import os
import re
import shutil

import frappe
from frappe import _
from frappe.utils import cint

from grid_interview.utils.settings import get_secret, get_settings

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,250}$")


def safe_key(key: str) -> str:
	if not _KEY_RE.match(key or "") or ".." in key or key.startswith("/"):
		raise ValueError(f"unsafe storage key: {key!r}")
	return key


class LocalStorage:
	name = "Local Private"

	def __init__(self):
		path = (get_settings().local_storage_path or "").strip()
		self.base = os.path.realpath(path or frappe.get_site_path("private", "grid_interview_recordings"))

	def path(self, key: str) -> str:
		full = os.path.realpath(os.path.join(self.base, safe_key(key)))
		if not full.startswith(self.base + os.sep):
			raise ValueError("storage key escapes the storage folder")
		return full

	def free_bytes(self) -> int:
		os.makedirs(self.base, mode=0o750, exist_ok=True)
		return shutil.disk_usage(self.base).free

	def put(self, src: str, key: str, content_type: str) -> int:
		dest = self.path(key)
		os.makedirs(os.path.dirname(dest), mode=0o750, exist_ok=True)
		shutil.move(src, dest)
		os.chmod(dest, 0o640)
		return os.path.getsize(dest)

	def exists(self, key: str) -> bool:
		return os.path.isfile(self.path(key))

	def size(self, key: str) -> int:
		return os.path.getsize(self.path(key))

	def local_copy(self, key: str) -> tuple[str, bool]:
		"""(path, is_temporary). Local files are read in place."""
		return self.path(key), False

	def delete(self, key: str) -> None:
		path = self.path(key)
		if os.path.exists(path):
			os.remove(path)


class S3Storage:
	name = "S3"

	def __init__(self):
		s = get_settings()
		if not (s.s3_bucket and s.s3_region):
			frappe.throw(_("S3 bucket and region are not configured."))
		import boto3

		kwargs = {"region_name": s.s3_region}
		if s.s3_endpoint_url:
			kwargs["endpoint_url"] = s.s3_endpoint_url
		if s.s3_access_key_id:  # otherwise the EC2 instance role is used
			kwargs["aws_access_key_id"] = s.s3_access_key_id
			kwargs["aws_secret_access_key"] = get_secret("s3_secret_access_key")
		self.client = boto3.client("s3", **kwargs)
		self.bucket = s.s3_bucket
		self.prefix = (s.s3_prefix or "").lstrip("/")
		self.ttl = cint(s.signed_url_ttl_seconds or 300)

	def _key(self, key: str) -> str:
		return self.prefix + safe_key(key)

	def free_bytes(self) -> int:
		return 1 << 62  # not a local-disk concern

	def put(self, src: str, key: str, content_type: str) -> int:
		size = os.path.getsize(src)
		self.client.upload_file(
			src, self.bucket, self._key(key),
			ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": content_type},
		)
		head = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
		if int(head["ContentLength"]) != size:  # integrity: S3 must hold exactly what we uploaded
			raise OSError("S3 object size does not match the uploaded file")
		os.remove(src)
		return size

	def exists(self, key: str) -> bool:
		try:
			self.client.head_object(Bucket=self.bucket, Key=self._key(key))
			return True
		except Exception:
			return False

	def size(self, key: str) -> int:
		return int(self.client.head_object(Bucket=self.bucket, Key=self._key(key))["ContentLength"])

	def local_copy(self, key: str) -> tuple[str, bool]:
		import tempfile

		fd, path = tempfile.mkstemp(prefix="gi-rec-", suffix=os.path.splitext(key)[1])
		os.close(fd)
		self.client.download_file(self.bucket, self._key(key), path)
		return path, True

	def presigned_url(self, key: str, filename: str, as_attachment: bool) -> str:
		disposition = f'{"attachment" if as_attachment else "inline"}; filename="{filename}"'
		return self.client.generate_presigned_url(
			"get_object",
			Params={"Bucket": self.bucket, "Key": self._key(key), "ResponseContentDisposition": disposition},
			ExpiresIn=self.ttl,
		)

	def delete(self, key: str) -> None:
		self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


def get_storage(name: str | None = None):
	name = name or get_settings().storage_backend or "Local Private"
	return {"Local Private": LocalStorage, "S3": S3Storage}[name]()
