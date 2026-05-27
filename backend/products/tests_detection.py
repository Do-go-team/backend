from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from ninja_jwt.tokens import AccessToken

from assets_3d.models import Asset3D, AssetGenerationTask
from products.models import ProductDetectionItem, ProductDetectionTask
from users.models import User


class DetectionItemRejectApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="detector@example.com",
            password="User1234!",
            name="Detector",
            confirmed=True,
        )
        self.other_user = User.objects.create_user(
            email="other-detector@example.com",
            password="User1234!",
            name="Other",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))
        self.other_access = str(AccessToken.for_user(self.other_user))
        self.task = ProductDetectionTask.objects.create(
            requested_by=self.user,
            status=ProductDetectionTask.Status.COMPLETED,
            callback_token="callback-token",
        )
        self._slot = 0

    def _url(self, task_id: int, item_id: int) -> str:
        return f"/api/v1/products/detection-tasks/{task_id}/items/{item_id}/reject"

    def _patch(self, task_id: int, item_id: int, access_token: str | None):
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            self._url(task_id, item_id),
            data={},
            content_type="application/json",
        )

    def _create_item(
        self,
        *,
        task: ProductDetectionTask | None = None,
        status: str = ProductDetectionItem.Status.DETECTED,
        asset_generation_status: str = ProductDetectionItem.AssetGenerationStatus.NOT_REQUESTED,
        asset_generation_task: AssetGenerationTask | None = None,
        asset_3d: Asset3D | None = None,
    ) -> ProductDetectionItem:
        slot = self._slot
        self._slot += 1
        return ProductDetectionItem.objects.create(
            task=task or self.task,
            slot=slot,
            thumbnail_key=f"detections/{self.task.id}/thumb_{slot}.png",
            relative_position_x=Decimal("0.100000"),
            relative_position_y=Decimal("0.200000"),
            relative_size_width=Decimal("0.300000"),
            relative_size_height=Decimal("0.400000"),
            status=status,
            asset_generation_status=asset_generation_status,
            asset_generation_task=asset_generation_task,
            asset_3d=asset_3d,
        )

    def test_reject_success_detected_not_requested(self):
        item = self._create_item()

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["detection_task_id"], self.task.id)
        self.assertEqual(body["data"]["detection_item_id"], item.id)
        self.assertEqual(body["data"]["status"], ProductDetectionItem.Status.REJECTED)
        item.refresh_from_db()
        self.assertEqual(item.status, ProductDetectionItem.Status.REJECTED)

    def test_reject_idempotent_when_already_rejected(self):
        item = self._create_item(status=ProductDetectionItem.Status.REJECTED)

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"]["status"], ProductDetectionItem.Status.REJECTED
        )

    def test_reject_fails_when_item_not_in_task(self):
        other_task = ProductDetectionTask.objects.create(
            requested_by=self.user,
            status=ProductDetectionTask.Status.COMPLETED,
            callback_token="other-token",
        )
        item = self._create_item(task=other_task)

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "DETECTION_ITEM_NOT_FOUND")

    def test_reject_fails_when_status_registered(self):
        item = self._create_item(status=ProductDetectionItem.Status.REGISTERED)

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_fails_when_asset_generation_status_pending(self):
        item = self._create_item(
            asset_generation_status=ProductDetectionItem.AssetGenerationStatus.PENDING
        )

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_fails_when_asset_generation_status_processing(self):
        item = self._create_item(
            asset_generation_status=ProductDetectionItem.AssetGenerationStatus.PROCESSING
        )

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_fails_when_asset_generation_status_completed(self):
        item = self._create_item(
            asset_generation_status=ProductDetectionItem.AssetGenerationStatus.COMPLETED
        )

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_fails_when_asset_3d_exists(self):
        asset = Asset3D.objects.create(
            target_type=Asset3D.TargetType.DETECTION_ITEM,
            target_id=999,
            file_format=Asset3D.FileFormat.PLY,
            model_url="https://example.com/model.ply",
        )
        item = self._create_item(asset_3d=asset)

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_fails_when_asset_generation_task_is_pending(self):
        asset_task = AssetGenerationTask.objects.create(
            target_type=AssetGenerationTask.TargetType.DETECTION_ITEM,
            target_id=1234,
            source_image_url="https://example.com/source.png",
            status=AssetGenerationTask.Status.PENDING,
        )
        item = self._create_item(asset_generation_task=asset_task)

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_fails_when_asset_generation_task_is_completed(self):
        asset_task = AssetGenerationTask.objects.create(
            target_type=AssetGenerationTask.TargetType.DETECTION_ITEM,
            target_id=1235,
            source_image_url="https://example.com/source.png",
            status=AssetGenerationTask.Status.COMPLETED,
        )
        item = self._create_item(asset_generation_task=asset_task)

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_DETECTION_ITEM_STATE")

    def test_reject_success_when_asset_generation_status_failed(self):
        item = self._create_item(
            status=ProductDetectionItem.Status.SELECTED,
            asset_generation_status=ProductDetectionItem.AssetGenerationStatus.FAILED,
        )

        response = self._patch(self.task.id, item.id, self.access)

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, ProductDetectionItem.Status.REJECTED)

    def test_reject_fails_for_unauthorized_user(self):
        item = self._create_item()

        response = self._patch(self.task.id, item.id, self.other_access)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "DETECTION_TASK_NOT_FOUND")


class DetectionGenerate3DRejectUnselectedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="generator@example.com",
            password="User1234!",
            name="Generator",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))
        self.task = ProductDetectionTask.objects.create(
            requested_by=self.user,
            status=ProductDetectionTask.Status.COMPLETED,
            callback_token="callback-token",
        )
        self._slot = 0

    def _post_generate_3d(self, payload: dict):
        self.client.cookies["access_token"] = self.access
        return self.client.post(
            f"/api/v1/products/detection-tasks/{self.task.id}/generate-3d",
            data=payload,
            content_type="application/json",
        )

    def _create_item(
        self,
        *,
        status: str = ProductDetectionItem.Status.DETECTED,
        asset_generation_status: str = ProductDetectionItem.AssetGenerationStatus.NOT_REQUESTED,
    ) -> ProductDetectionItem:
        slot = self._slot
        self._slot += 1
        return ProductDetectionItem.objects.create(
            task=self.task,
            slot=slot,
            thumbnail_key=f"detections/{self.task.id}/thumb_{slot}.png",
            relative_position_x=Decimal("0.100000"),
            relative_position_y=Decimal("0.200000"),
            relative_size_width=Decimal("0.300000"),
            relative_size_height=Decimal("0.400000"),
            status=status,
            asset_generation_status=asset_generation_status,
        )

    @patch(
        "products.detection_services._build_thumbnail_source_image_url",
        return_value="https://example.com/thumb.png",
    )
    def test_reject_unselected_only_rejects_allowed_items(self, _mock_source_url):
        selected = self._create_item()
        rejectable = self._create_item()
        blocked_processing = self._create_item(
            asset_generation_status=ProductDetectionItem.AssetGenerationStatus.PROCESSING
        )

        response = self._post_generate_3d(
            {
                "selected_item_ids": [selected.id],
                "reject_unselected": True,
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["selected_item_ids"], [selected.id])
        self.assertIn(rejectable.id, data["rejected_item_ids"])
        self.assertIn(blocked_processing.id, data["skipped_reject_item_ids"])

        selected.refresh_from_db()
        rejectable.refresh_from_db()
        blocked_processing.refresh_from_db()

        self.assertEqual(selected.status, ProductDetectionItem.Status.SELECTED)
        self.assertEqual(
            selected.asset_generation_status,
            ProductDetectionItem.AssetGenerationStatus.PENDING,
        )
        self.assertIsNotNone(selected.asset_generation_task_id)

        self.assertEqual(rejectable.status, ProductDetectionItem.Status.REJECTED)
        self.assertEqual(
            blocked_processing.status,
            ProductDetectionItem.Status.DETECTED,
        )
        self.assertEqual(
            blocked_processing.asset_generation_status,
            ProductDetectionItem.AssetGenerationStatus.PROCESSING,
        )
