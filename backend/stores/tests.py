import json
import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from ninja_jwt.tokens import AccessToken

from stores.models import Store, StoreImage, StoreMember
from users.models import User


class StoreDetailEndpointTests(TestCase):
    """GET /api/v1/stores/{store_id} — detail view for store members only.

    Spec collapses "store missing" and "user has no access" into a single
    404 STORE_NOT_FOUND so non-members cannot probe IDs. Tests below lock
    that behavior in explicitly.
    """

    url_tmpl = "/api/v1/stores/{store_id}"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="Owner123!",
            name="오너",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="staff@example.com",
            password="Staff123!",
            name="스태프",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            password="Out123!",
            name="외부",
            confirmed=True,
        )

        self.store = Store.objects.create(
            user=self.owner,
            name="DO-GO 성수 팝업",
            address="서울 성동구",
            max_admin_count=5,
            width=600,
            height=350,
            depth=500,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _get(self, store_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url_tmpl.format(store_id=store_id), **headers)

    def test_success_owner_returns_full_detail(self):
        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "매장 상세 정보 조회에 성공했습니다.")

        data = body["data"]
        self.assertEqual(data["store_id"], self.store.id)
        self.assertEqual(data["name"], "DO-GO 성수 팝업")
        self.assertEqual(data["width"], 600)
        self.assertEqual(data["height"], 350)
        self.assertEqual(data["depth"], 500)
        self.assertEqual(data["my_role"], StoreMember.Role.OWNER)
        self.assertIsNone(data["floorplan_image_url"])
        self.assertIsNone(data["actual_photo_url"])
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)

        # Lock the projection so future refactors can't leak fields
        self.assertEqual(
            set(data.keys()),
            {
                "store_id",
                "name",
                "width",
                "height",
                "depth",
                "my_role",
                "floorplan_image_url",
                "actual_photo_url",
                "created_at",
                "updated_at",
            },
        )

    def test_success_non_owner_member_returns_own_role(self):
        """STAFF/VMD/MANAGER — any membership grants read access. my_role
        must reflect the requesting user's membership, not the store owner's."""
        response = self._get(self.store.id, self.staff_access)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["my_role"], StoreMember.Role.STAFF)

    def test_non_member_returns_store_not_found(self):
        """Security: outsiders must not be able to distinguish between
        'store exists but I can't see it' and 'store doesn't exist'."""
        response = self._get(self.store.id, self.outsider_access)

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "STORE_NOT_FOUND")
        self.assertEqual(
            body["message"], "존재하지 않거나 접근 권한이 없는 매장입니다."
        )

    def test_nonexistent_store_returns_store_not_found(self):
        """Same error shape as the non-member case — intentional."""
        response = self._get(999999, self.owner_access)

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        """Soft-deleted stores must be invisible even to their own members."""
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_authorization_header(self):
        response = self._get(self.store.id)

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED_USER")

    def test_unauthorized_with_malformed_token(self):
        response = self._get(self.store.id, "not-a-real-token")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_returns_image_urls_when_present(self):
        """Picks the first FLOORPLAN and first ACTUAL_PHOTO row. Matches
        list_my_stores behavior for cross-endpoint consistency."""
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
            image_url="stores/images/floor.png",
        )
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.ACTUAL_PHOTO,
            image_url="stores/images/photo.jpg",
        )

        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["floorplan_image_url"].endswith("floor.png"))
        self.assertTrue(data["actual_photo_url"].endswith("photo.jpg"))


class StoreUpdateEndpointTests(TestCase):
    """PATCH /api/v1/stores/{store_id} — OWNER/MANAGER only.

    Non-members still see STORE_NOT_FOUND (same as GET — no ID probing).
    STAFF/VMD are members, so leaking their role gap via a distinct error
    (FORBIDDEN_ACCESS) is acceptable and matches the spec.
    """

    url_tmpl = "/api/v1/stores/{store_id}"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="up_owner@example.com",
            password="X1!abcde",
            name="오너",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="up_manager@example.com",
            password="X1!abcde",
            name="매니저",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="up_staff@example.com",
            password="X1!abcde",
            name="스태프",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="up_vmd@example.com",
            password="X1!abcde",
            name="VMD",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="up_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="DO-GO 성수",
            address="서울 성동구",
            width=600,
            height=350,
            depth=500,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _patch(self, store_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            self.url_tmpl.format(store_id=store_id),
            data=json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def test_success_owner_partial_update_preserves_untouched_fields(self):
        """PATCH semantics: only sent fields change; the rest stay."""
        response = self._patch(
            self.store.id,
            {"width": 650, "height": 360},
            self.owner_access,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "매장 정보가 성공적으로 수정되었습니다.")

        self.store.refresh_from_db()
        self.assertEqual(self.store.width, 650)
        self.assertEqual(self.store.height, 360)
        self.assertEqual(self.store.depth, 500)  # untouched
        self.assertEqual(self.store.name, "DO-GO 성수")  # untouched
        self.assertEqual(self.store.address, "서울 성동구")

        data = body["data"]
        self.assertEqual(
            set(data.keys()),
            {"store_id", "name", "address", "width", "height", "depth", "updated_at"},
        )
        self.assertEqual(data["width"], 650)

    def test_success_manager_can_update(self):
        response = self._patch(
            self.store.id,
            {"name": "DO-GO 성수 (확장)"},
            self.manager_access,
        )
        self.assertEqual(response.status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(self.store.name, "DO-GO 성수 (확장)")

    def test_staff_forbidden(self):
        response = self._patch(self.store.id, {"name": "x"}, self.staff_access)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")
        self.assertEqual(body["message"], "매장 정보를 수정할 권한이 없습니다.")

        self.store.refresh_from_db()
        self.assertEqual(self.store.name, "DO-GO 성수")  # unchanged

    def test_vmd_can_update(self):
        """VMD 도 ADMIN_ROLES (STAFF만 제외) 에 포함됨. 매장 정보 수정 가능."""
        response = self._patch(self.store.id, {"name": "VMD-수정"}, self.vmd_access)
        self.assertEqual(response.status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(self.store.name, "VMD-수정")

    def test_outsider_returns_store_not_found(self):
        """Non-members must not be distinguishable from 'store doesn't exist'."""
        response = self._patch(self.store.id, {"name": "x"}, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._patch(999999, {"name": "x"}, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        from django.utils import timezone

        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._patch(self.store.id, {"name": "x"}, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._patch(self.store.id, {"name": "x"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_empty_body_is_noop(self):
        """Empty body should succeed without changing anything — defensive
        no-op so the endpoint can't be used to, say, bump updated_at casually."""
        before = self.store.updated_at
        response = self._patch(self.store.id, {}, self.owner_access)

        self.assertEqual(response.status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(self.store.updated_at, before)

    def test_image_create_when_absent(self):
        self.assertFalse(StoreImage.objects.filter(store=self.store).exists())
        response = self._patch(
            self.store.id,
            {
                "floorplan_image_url": "https://cdn.example.com/stores/images/new-floor.png"
            },
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        images = StoreImage.objects.filter(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
        )
        self.assertEqual(images.count(), 1)
        self.assertTrue(images.first().image_url.name.endswith("new-floor.png"))

    def test_image_update_when_present(self):
        """update_or_create must update the existing row, not insert a duplicate."""
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
            image_url="stores/images/old.png",
        )
        response = self._patch(
            self.store.id,
            {"floorplan_image_url": "https://cdn.example.com/stores/images/new.png"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        images = StoreImage.objects.filter(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
        )
        self.assertEqual(images.count(), 1)  # not duplicated
        self.assertTrue(images.first().image_url.name.endswith("new.png"))

    def test_image_delete_when_null(self):
        """Null signal clears the image of that type only; the other type stays."""
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
            image_url="stores/images/floor.png",
        )
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.ACTUAL_PHOTO,
            image_url="stores/images/photo.jpg",
        )
        response = self._patch(
            self.store.id,
            {"floorplan_image_url": None},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            StoreImage.objects.filter(
                store=self.store,
                image_type=StoreImage.ImageType.FLOORPLAN,
            ).exists()
        )
        # ACTUAL_PHOTO must survive
        self.assertTrue(
            StoreImage.objects.filter(
                store=self.store,
                image_type=StoreImage.ImageType.ACTUAL_PHOTO,
            ).exists()
        )

    def test_image_key_omission_is_ignored(self):
        """PATCH with no image key must not touch existing image rows."""
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
            image_url="stores/images/keep.png",
        )
        response = self._patch(self.store.id, {"name": "새이름"}, self.owner_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            StoreImage.objects.filter(
                store=self.store,
                image_type=StoreImage.ImageType.FLOORPLAN,
            ).count(),
            1,
        )

    def test_max_admin_count_not_accepted(self):
        """Product decision: max_admin_count is not user-editable via PATCH.
        The field is dropped from the schema — sending it should be ignored
        silently by ninja's schema parser (unknown key)."""
        before = self.store.max_admin_count
        response = self._patch(
            self.store.id,
            {"max_admin_count": 999},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(self.store.max_admin_count, before)


class StoreDeleteEndpointTests(TestCase):
    """DELETE /api/v1/stores/{store_id} — MANAGER (점장) only.

    매장 등록자 = MANAGER 정책에 따라, 점장만 매장을 삭제할 수 있음.
    부점장(VICE_MANAGER) / VMD / STAFF 모두 거부. Soft-deletes the store
    and cascades the same to its layouts so a deleted store can't leave
    stranded children visible to clients.
    """

    url_tmpl = "/api/v1/stores/{store_id}"

    def setUp(self):
        self.manager = User.objects.create_user(
            email="del_manager@example.com",
            password="X1!abcde",
            name="점장",
            confirmed=True,
        )
        self.vice = User.objects.create_user(
            email="del_vice@example.com",
            password="X1!abcde",
            name="부점장",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="del_staff@example.com",
            password="X1!abcde",
            name="스태프",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="del_vmd@example.com",
            password="X1!abcde",
            name="VMD",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="del_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.manager,
            name="DEL-base",
            address="서울",
            width=600,
            height=350,
            depth=500,
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.vice, role=StoreMember.Role.VICE_MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )

        self.manager_access = str(AccessToken.for_user(self.manager))
        self.vice_access = str(AccessToken.for_user(self.vice))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _delete(self, store_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.delete(
            self.url_tmpl.format(store_id=store_id),
            **headers,
        )

    def test_success_manager_soft_deletes_store(self):
        response = self._delete(self.store.id, self.manager_access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "매장이 성공적으로 삭제되었습니다.")
        self.assertIsNone(body["data"])

        self.store.refresh_from_db()
        self.assertIsNotNone(self.store.deleted_at)

        # Row not actually removed — soft delete only
        self.assertTrue(Store.objects.filter(id=self.store.id).exists())

    def test_vice_manager_forbidden(self):
        """부점장은 매장 삭제 불가 — destructive op 는 점장 전용."""
        response = self._delete(self.store.id, self.vice_access)

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")
        self.assertIn("점장", body["message"])

        self.store.refresh_from_db()
        self.assertIsNone(self.store.deleted_at)  # untouched

    def test_staff_forbidden(self):
        response = self._delete(self.store.id, self.staff_access)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "FORBIDDEN_ACCESS")

    def test_vmd_forbidden(self):
        response = self._delete(self.store.id, self.vmd_access)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "FORBIDDEN_ACCESS")

    def test_outsider_returns_store_not_found(self):
        """Same enumeration-prevention policy as GET/PATCH."""
        response = self._delete(self.store.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._delete(999999, self.manager_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_already_deleted_store_returns_store_not_found(self):
        """Idempotency: second DELETE on the same store can't succeed —
        membership lookup excludes soft-deleted stores."""
        self.store.soft_delete()

        response = self._delete(self.store.id, self.manager_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._delete(self.store.id)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_subsequent_get_returns_not_found(self):
        """Regression: after DELETE, the same store_id must be invisible
        to GET (soft-deleted stores aren't enumerable)."""
        self._delete(self.store.id, self.manager_access)

        self.client.cookies["access_token"] = self.manager_access
        response = self.client.get(
            self.url_tmpl.format(store_id=self.store.id),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_cascades_layout_soft_delete(self):
        """Spec recommendation: layouts under a deleted store must also be
        soft-deleted in the same transaction so clients can't see orphan layouts."""
        from layouts.models import Layout

        live_layout = Layout.objects.create(
            store=self.store, name="라이브", is_active=True
        )
        already_dead = Layout.objects.create(store=self.store, name="기삭제")
        already_dead.soft_delete()
        prior_deleted_at = already_dead.deleted_at

        self._delete(self.store.id, self.manager_access)

        live_layout.refresh_from_db()
        self.assertIsNotNone(live_layout.deleted_at)

        # Re-deleting an already-dead layout must not bump its deleted_at
        already_dead.refresh_from_db()
        self.assertEqual(already_dead.deleted_at, prior_deleted_at)

    def test_does_not_touch_other_stores_layouts(self):
        """Cascade must be scoped to the deleted store, not all stores."""
        from layouts.models import Layout

        other_store = Store.objects.create(
            user=self.manager,
            name="다른매장",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(
            store=other_store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        bystander = Layout.objects.create(store=other_store, name="bystander")

        self._delete(self.store.id, self.manager_access)

        bystander.refresh_from_db()
        self.assertIsNone(bystander.deleted_at)


class StoreProductsAssignEndpointTests(TestCase):
    """POST /api/v1/stores/{store_id}/products — bulk-assign product masters.

    Encodes the policy decisions from the spec discussion:
      - OWNER + MANAGER allowed (STAFF/VMD/outsider blocked)
      - Strict invalid-id rejection (entire request fails with 422)
      - PAUSED → ACTIVE reactivation counts toward assigned_count
      - total_count covers ACTIVE + PAUSED (broad "취급 중" reading)
      - StoreInventory cascade with stock_quantity=0 per variant
    """

    url_tmpl = "/api/v1/stores/{store_id}/products"

    def setUp(self):
        import json as _json
        from products.models import ProductMaster, ProductVariant

        self._json = _json
        self._ProductMaster = ProductMaster
        self._ProductVariant = ProductVariant

        self.owner = User.objects.create_user(
            email="ap_owner@example.com",
            password="X1!abcde",
            name="오너",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="ap_manager@example.com",
            password="X1!abcde",
            name="매니저",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="ap_staff@example.com",
            password="X1!abcde",
            name="스태프",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="ap_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="AP-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        # Two products. p1 has 2 variants, p2 has 1 variant.
        # The "any user can assign" decision (#4) is exercised by registering
        # products under self.outsider (a different user from store members).
        self.p1 = ProductMaster.objects.create(
            user=self.outsider,
            name="P1",
            price=1000,
            width=10,
            height=10,
            depth=10,
        )
        self.p1_v1 = ProductVariant.objects.create(
            product_master=self.p1,
            size="S",
            sku_code="P1-S",
        )
        self.p1_v2 = ProductVariant.objects.create(
            product_master=self.p1,
            size="M",
            sku_code="P1-M",
        )
        self.p2 = ProductMaster.objects.create(
            user=self.outsider,
            name="P2",
            price=2000,
            width=20,
            height=20,
            depth=20,
        )
        self.p2_v1 = ProductVariant.objects.create(
            product_master=self.p2,
            size="S",
            sku_code="P2-S",
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _post(self, store_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url_tmpl.format(store_id=store_id),
            data=self._json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def test_success_owner_assigns_two_products_with_inventory_cascade(self):
        from products.models import StoreInventory, StoreProduct

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id, self.p2.id]},
            self.owner_access,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(
            body["message"], "선택한 상품들이 매장에 성공적으로 할당되었습니다."
        )
        data = body["data"]
        self.assertEqual(data["assigned_count"], 2)
        self.assertEqual(data["total_count"], 2)
        self.assertEqual(set(data.keys()), {"assigned_count", "total_count"})

        # StoreProduct rows
        self.assertEqual(StoreProduct.objects.filter(store=self.store).count(), 2)
        # Inventory cascade — 3 variants total (2 from p1 + 1 from p2)
        self.assertEqual(
            StoreInventory.objects.filter(store=self.store).count(),
            3,
        )
        for inv in StoreInventory.objects.filter(store=self.store):
            self.assertEqual(inv.stock_quantity, 0)

    def test_manager_can_assign(self):
        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.manager_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["assigned_count"], 1)

    def test_staff_forbidden(self):
        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.staff_access,
        )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")
        self.assertIn("OWNER/MANAGER", body["message"])

    def test_outsider_returns_store_not_found(self):
        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.outsider_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._post(self.store.id, {"product_ids": [self.p1.id]})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_invalid_product_id_rejects_entire_request(self):
        """Strict mode: one bad ID poisons the whole request — no partial
        commit. Avoids silent drops that frontends could mistake for bugs."""
        from products.models import StoreInventory, StoreProduct

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id, 999_999]},
            self.owner_access,
        )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "INVALID_PRODUCT_IDS")
        self.assertIn("999999", body["message"])

        # Atomicity: p1 must NOT be assigned despite being valid
        self.assertEqual(StoreProduct.objects.filter(store=self.store).count(), 0)
        self.assertEqual(StoreInventory.objects.filter(store=self.store).count(), 0)

    def test_soft_deleted_product_treated_as_invalid(self):
        self.p2.soft_delete()

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id, self.p2.id]},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PRODUCT_IDS")

    def test_paused_assignment_reactivates_and_counts(self):
        """Decision #1: PAUSED → ACTIVE on re-assign, included in assigned_count."""
        from products.models import StoreProduct

        StoreProduct.objects.create(
            store=self.store,
            product_master=self.p1,
            status=StoreProduct.Status.PAUSED,
        )

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.owner_access,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["assigned_count"], 1)

        sp = StoreProduct.objects.get(store=self.store, product_master=self.p1)
        self.assertEqual(sp.status, StoreProduct.Status.ACTIVE)

    def test_already_active_not_double_counted(self):
        """ACTIVE → ACTIVE: no-op, not in assigned_count (avoids inflated metrics)."""
        from products.models import StoreProduct

        StoreProduct.objects.create(
            store=self.store,
            product_master=self.p1,
            status=StoreProduct.Status.ACTIVE,
        )

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.owner_access,
        )
        self.assertEqual(response.json()["data"]["assigned_count"], 0)
        self.assertEqual(response.json()["data"]["total_count"], 1)

    def test_total_count_includes_paused(self):
        """Decision #5: PAUSED counts toward total_count too — ERD calls it
        '취급 상태', so PAUSED is still 'a kind of 취급'."""
        from products.models import StoreProduct

        # Pre-existing PAUSED assignment that we don't touch this round
        StoreProduct.objects.create(
            store=self.store,
            product_master=self.p2,
            status=StoreProduct.Status.PAUSED,
        )

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.owner_access,
        )

        # assigned: just p1 (1). total: p1 ACTIVE + p2 PAUSED = 2
        self.assertEqual(response.json()["data"]["assigned_count"], 1)
        self.assertEqual(response.json()["data"]["total_count"], 2)

    def test_empty_product_ids_is_noop(self):
        response = self._post(
            self.store.id,
            {"product_ids": []},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["assigned_count"], 0)
        self.assertEqual(response.json()["data"]["total_count"], 0)

    def test_duplicate_ids_deduplicated(self):
        """[p1, p1, p1] should behave identically to [p1] — set semantics."""
        from products.models import StoreProduct

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id, self.p1.id, self.p1.id]},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["assigned_count"], 1)
        self.assertEqual(StoreProduct.objects.filter(store=self.store).count(), 1)

    def test_inventory_cascade_idempotent(self):
        """Re-assigning the same product must not duplicate inventory rows."""
        from products.models import StoreInventory

        self._post(self.store.id, {"product_ids": [self.p1.id]}, self.owner_access)
        self._post(self.store.id, {"product_ids": [self.p1.id]}, self.owner_access)

        # p1 has 2 variants — exactly 2 inventory rows must exist
        self.assertEqual(
            StoreInventory.objects.filter(store=self.store).count(),
            2,
        )

    def test_soft_deleted_store_returns_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_other_users_product_can_be_assigned(self):
        """Decision #4: product_masters.user_id is audit-only, not a tenant
        boundary. Any store can assign any product (HQ-warehouse model)."""
        from products.models import StoreProduct

        # All seed products are owned by self.outsider; owner is assigning
        response = self._post(
            self.store.id,
            {"product_ids": [self.p1.id]},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            StoreProduct.objects.filter(
                store=self.store,
                product_master=self.p1,
            ).exists()
        )


class StoreProductsListEndpointTests(TestCase):
    """GET /api/v1/stores/{store_id}/products — handled product catalog.

    Permission is plain membership (any role) — same as GET /stores/{id}.
    Output decisions encoded:
      - Soft-deleted product → status='DISCONTINUED' (synthesized at response,
        not stored in DB)
      - Soft-deleted variant → kept in response with is_discontinued=true
      - Missing inventory row → stock_quantity=0 fallback (spec is required)
      - Multiple Asset3D rows → latest by created_at wins
    """

    url_tmpl = "/api/v1/stores/{store_id}/products"

    def setUp(self):
        from products.models import (
            ProductMaster,
            ProductVariant,
            StoreInventory,
            StoreProduct,
        )

        self._ProductMaster = ProductMaster
        self._ProductVariant = ProductVariant
        self._StoreProduct = StoreProduct
        self._StoreInventory = StoreInventory

        self.owner = User.objects.create_user(
            email="lp_owner@example.com",
            password="X1!abcde",
            name="o",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="lp_staff@example.com",
            password="X1!abcde",
            name="s",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="lp_out@example.com",
            password="X1!abcde",
            name="x",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="LP-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _get(self, store_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url_tmpl.format(store_id=store_id), **headers)

    def _make_product(self, name, price=1000, image_url=None):
        return self._ProductMaster.objects.create(
            user=self.outsider,
            name=name,
            price=price,
            width=10,
            height=10,
            depth=10,
            image_url=image_url,
        )

    def _assign(self, product, status=None):
        if status is None:
            status = self._StoreProduct.Status.ACTIVE
        return self._StoreProduct.objects.create(
            store=self.store,
            product_master=product,
            status=status,
        )

    def test_empty_when_no_products_assigned(self):
        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "매장 취급 상품 목록 조회에 성공했습니다.")
        self.assertEqual(body["data"], {"products": []})

    def test_active_product_with_variants_and_inventory(self):
        p = self._make_product("티셔츠", price=25000)
        v1 = self._ProductVariant.objects.create(
            product_master=p,
            size="M",
            color="Black",
            sku_code="TS-M",
        )
        v2 = self._ProductVariant.objects.create(
            product_master=p,
            size="L",
            color="Black",
            sku_code="TS-L",
        )
        self._assign(p)
        self._StoreInventory.objects.create(
            store=self.store, variant=v1, stock_quantity=50
        )
        self._StoreInventory.objects.create(
            store=self.store, variant=v2, stock_quantity=30
        )

        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        products = response.json()["data"]["products"]
        self.assertEqual(len(products), 1)

        prod = products[0]
        self.assertEqual(prod["id"], p.id)
        self.assertEqual(prod["name"], "티셔츠")
        self.assertEqual(prod["price"], 25000)
        self.assertEqual(prod["status"], "ACTIVE")
        self.assertIsNone(prod["model_url"])
        self.assertEqual(
            set(prod.keys()),
            {
                "id",
                "name",
                "price",
                "status",
                "width",
                "height",
                "depth",
                "image_url",
                "model_url",
                "variants",
            },
        )

        # Variants — both alive, both with inventory
        self.assertEqual(len(prod["variants"]), 2)
        v_by_size = {v["size"]: v for v in prod["variants"]}
        self.assertEqual(v_by_size["M"]["stock_quantity"], 50)
        self.assertEqual(v_by_size["L"]["stock_quantity"], 30)
        self.assertFalse(v_by_size["M"]["is_discontinued"])
        self.assertEqual(
            set(v_by_size["M"].keys()),
            {
                "id",
                "size",
                "color",
                "sku_code",
                "barcode_image_url",
                "stock_quantity",
                "is_discontinued",
            },
        )

    def test_image_url_url_field_returns_raw_string(self):
        """163 의 URLField 전환 후 회귀 — 이전 ImageField 시절 .url 호출이
        URLField 에선 AttributeError 였음. raw string 그대로 응답되는지 검증."""
        p = self._make_product(
            "URL 이미지 상품",
            image_url="https://s3.example.com/master.jpg",
        )
        self._assign(p)

        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        prod = response.json()["data"]["products"][0]
        self.assertEqual(prod["image_url"], "https://s3.example.com/master.jpg")

    def test_paused_status_passes_through(self):
        p = self._make_product("일시중지품")
        self._assign(p, status=self._StoreProduct.Status.PAUSED)

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.json()["data"]["products"][0]["status"], "PAUSED")

    def test_soft_deleted_product_master_appears_as_discontinued(self):
        """Decision #1: HQ soft-delete surfaces as DISCONTINUED in the response.
        DB still holds ACTIVE/PAUSED — the product_master.deleted_at is the
        source of truth, synthesized at output."""
        p = self._make_product("단종품")
        self._assign(p, status=self._StoreProduct.Status.PAUSED)
        p.soft_delete()

        response = self._get(self.store.id, self.owner_access)
        prod = response.json()["data"]["products"][0]
        self.assertEqual(prod["status"], "DISCONTINUED")

        # DB row unchanged
        sp = self._StoreProduct.objects.get(store=self.store, product_master=p)
        self.assertEqual(sp.status, self._StoreProduct.Status.PAUSED)

    def test_soft_deleted_variant_marked_is_discontinued(self):
        """Decision #2: Discontinued variant stays in response with the
        marker, so the FE can render it differently rather than have it vanish."""
        p = self._make_product("일부단종")
        v_alive = self._ProductVariant.objects.create(
            product_master=p,
            size="S",
            sku_code="A-S",
        )
        v_dead = self._ProductVariant.objects.create(
            product_master=p,
            size="M",
            sku_code="A-M",
        )
        v_dead.soft_delete()
        self._assign(p)

        response = self._get(self.store.id, self.owner_access)
        prod = response.json()["data"]["products"][0]
        v_by_id = {v["id"]: v for v in prod["variants"]}

        self.assertFalse(v_by_id[v_alive.id]["is_discontinued"])
        self.assertTrue(v_by_id[v_dead.id]["is_discontinued"])
        self.assertEqual(len(prod["variants"]), 2)  # both present

    def test_missing_inventory_falls_back_to_zero(self):
        """Decision #3: stock_quantity stays a required int; missing inventory
        rows surface as 0 (distinguishable from 'not assigned' only by spec
        contract — at this endpoint, 0 means 'no stock')."""
        p = self._make_product("재고없는품")
        self._ProductVariant.objects.create(
            product_master=p,
            size="Free",
            sku_code="F-1",
        )
        self._assign(p)
        # Note: no StoreInventory row created on purpose

        response = self._get(self.store.id, self.owner_access)
        variants = response.json()["data"]["products"][0]["variants"]
        self.assertEqual(variants[0]["stock_quantity"], 0)

    def test_asset_3d_returns_latest_when_multiple(self):
        """Decision #4: ORDER BY created_at DESC, take the first per product.
        Multi-format support (GLB + OBJ for the same product) handled by picking
        the most recently registered."""
        from assets_3d.models import Asset3D

        p = self._make_product("3D상품")
        self._assign(p)
        # Create older one first, then newer
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=p.id,
            file_format=Asset3D.FileFormat.OBJ,
            model_url="assets/3d/old.obj",
        )
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=p.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/new.glb",
        )

        response = self._get(self.store.id, self.owner_access)
        model_url = response.json()["data"]["products"][0]["model_url"]
        self.assertTrue(model_url.endswith("new.glb"))
        # The other asset must NOT be in the response
        self.assertNotIn("old.obj", model_url)

    def test_asset_3d_for_different_target_type_ignored(self):
        """Make sure FIXTURE / STORE assets don't leak into product responses
        even when target_id collides numerically."""
        from assets_3d.models import Asset3D

        p = self._make_product("FK충돌")
        self._assign(p)
        # Same id but different target_type — must be ignored
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=p.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/wrong.glb",
        )

        response = self._get(self.store.id, self.owner_access)
        self.assertIsNone(response.json()["data"]["products"][0]["model_url"])

    def test_staff_can_view(self):
        """Plain membership (any role) is enough — same as GET /stores/{id}."""
        p = self._make_product("스탭조회")
        self._assign(p)

        response = self._get(self.store.id, self.staff_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]["products"]), 1)

    def test_outsider_returns_store_not_found(self):
        response = self._get(self.store.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._get(self.store.id)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_soft_deleted_store_returns_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_other_stores_inventory_does_not_leak(self):
        """If product is assigned to two stores with different stock,
        each store's response shows only its own inventory."""
        from products.models import StoreInventory

        p = self._make_product("멀티매장")
        v = self._ProductVariant.objects.create(
            product_master=p,
            size="F",
            sku_code="M-F",
        )
        self._assign(p)
        StoreInventory.objects.create(store=self.store, variant=v, stock_quantity=10)

        # Other store with the same product but different stock
        other_store = Store.objects.create(
            user=self.outsider,
            name="다른매장",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        self._StoreProduct.objects.create(
            store=other_store,
            product_master=p,
        )
        StoreInventory.objects.create(store=other_store, variant=v, stock_quantity=999)

        response = self._get(self.store.id, self.owner_access)
        variants = response.json()["data"]["products"][0]["variants"]
        self.assertEqual(variants[0]["stock_quantity"], 10)
        self.assertNotEqual(variants[0]["stock_quantity"], 999)


class StoreProductUpdateEndpointTests(TestCase):
    """PATCH /api/v1/stores/{store_id}/products/{product_id}

    OWNER/MANAGER only. Toggles store-side handling status (ACTIVE/PAUSED).
    Spec error codes: FORBIDDEN_ACCESS (403), PRODUCT_NOT_ASSIGNED (404).

    Per 156 design: DISCONTINUED is a GET-time overlay derived from
    product_master.deleted_at and orthogonal to the store's own status.
    PATCH operates on the DB-stored value and is allowed even when the
    master is soft-deleted (running-down-stock scenario).
    """

    url_tmpl = "/api/v1/stores/{store_id}/products/{product_id}"

    def setUp(self):
        from products.models import ProductMaster, StoreProduct

        self._json = json
        self._ProductMaster = ProductMaster
        self._StoreProduct = StoreProduct

        self.owner = User.objects.create_user(
            email="up_p_owner@example.com",
            password="X1!abcde",
            name="o",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="up_p_manager@example.com",
            password="X1!abcde",
            name="m",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="up_p_staff@example.com",
            password="X1!abcde",
            name="s",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="up_p_vmd@example.com",
            password="X1!abcde",
            name="v",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="up_p_out@example.com",
            password="X1!abcde",
            name="x",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="UP-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )

        self.product = ProductMaster.objects.create(
            user=self.outsider,
            name="P",
            price=1000,
            width=10,
            height=10,
            depth=10,
        )
        # Assignment lives in DB as ACTIVE — the typical starting state.
        self.assignment = StoreProduct.objects.create(
            store=self.store,
            product_master=self.product,
            status=StoreProduct.Status.ACTIVE,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _patch(self, store_id, product_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            self.url_tmpl.format(store_id=store_id, product_id=product_id),
            data=self._json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def test_success_owner_active_to_paused(self):
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.owner_access,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(
            body["message"], "매장 상품의 취급 상태가 성공적으로 수정되었습니다."
        )

        data = body["data"]
        self.assertEqual(data["product_id"], self.product.id)
        self.assertEqual(data["status"], "PAUSED")
        self.assertIn("updated_at", data)
        # Lock projection so future refactors can't leak fields
        self.assertEqual(set(data.keys()), {"product_id", "status", "updated_at"})

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, self._StoreProduct.Status.PAUSED)

    def test_success_paused_to_active(self):
        self.assignment.status = self._StoreProduct.Status.PAUSED
        self.assignment.save(update_fields=["status"])

        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "ACTIVE"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "ACTIVE")

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, self._StoreProduct.Status.ACTIVE)

    def test_success_manager_can_update(self):
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.manager_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, self._StoreProduct.Status.PAUSED)

    def test_staff_forbidden_with_spec_message(self):
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.staff_access,
        )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")
        self.assertEqual(
            body["message"], "해당 매장의 상품 정보를 수정할 권한이 없습니다."
        )

        self.assignment.refresh_from_db()
        self.assertEqual(
            self.assignment.status, self._StoreProduct.Status.ACTIVE
        )  # unchanged

    def test_vmd_can_update(self):
        """VMD 도 ADMIN_ROLES 에 포함 — 상품 status 수정 가능."""
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.vmd_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, self._StoreProduct.Status.PAUSED)

    def test_outsider_returns_store_not_found(self):
        """Same enumeration-prevention policy as the rest of the domain."""
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.outsider_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._patch(
            999_999,
            self.product.id,
            {"status": "PAUSED"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_product_not_assigned_returns_404(self):
        """Product master exists but the store has no assignment row."""
        unassigned = self._ProductMaster.objects.create(
            user=self.outsider,
            name="없는것",
            price=1000,
            width=1,
            height=1,
            depth=1,
        )
        response = self._patch(
            self.store.id,
            unassigned.id,
            {"status": "PAUSED"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "PRODUCT_NOT_ASSIGNED")
        self.assertEqual(body["message"], "해당 매장에 할당되어 있지 않은 상품입니다.")

    def test_nonexistent_product_returns_product_not_assigned(self):
        """Product master id never existed — same shape as 'never assigned'."""
        response = self._patch(
            self.store.id,
            999_999,
            {"status": "PAUSED"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_ASSIGNED")

    def test_invalid_status_value_rejected(self):
        """Any status outside ACTIVE/PAUSED is rejected by Ninja's schema
        layer with the project-wide INVALID_PARAMETER envelope (422)."""
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "FOO"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    def test_discontinued_value_rejected(self):
        """DISCONTINUED is a GET-time overlay only; FE attempting to write
        it must be rejected at the schema layer."""
        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "DISCONTINUED"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    def test_no_op_same_status_does_not_bump_updated_at(self):
        """Sending the current value must skip the save so PATCH cannot
        be abused as a 'touch' that bumps updated_at."""
        self.assignment.refresh_from_db()
        before = self.assignment.updated_at

        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "ACTIVE"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "ACTIVE")

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.updated_at, before)

    def test_discontinued_master_still_patchable(self):
        """156 contract: master.deleted_at is orthogonal to store.status.
        A store running down stock must still be able to PAUSE the display
        even after HQ has discontinued the product. PATCH echoes the actual
        DB-stored value; GET continues to overlay DISCONTINUED."""
        self.product.soft_delete()

        response = self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        # PATCH response: DB truth (PAUSED), not the overlay (DISCONTINUED)
        self.assertEqual(response.json()["data"]["status"], "PAUSED")

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, self._StoreProduct.Status.PAUSED)

    def test_other_stores_assignment_not_touched(self):
        """Cross-store isolation: PATCH on store A's product must not
        affect another store's assignment of the same product master."""
        other_owner = User.objects.create_user(
            email="up_p_other@example.com",
            password="X1!abcde",
            name="oo",
            confirmed=True,
        )
        other_store = Store.objects.create(
            user=other_owner,
            name="다른매장",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(
            store=other_store,
            user=other_owner,
            role=StoreMember.Role.OWNER,
        )
        other_assignment = self._StoreProduct.objects.create(
            store=other_store,
            product_master=self.product,
            status=self._StoreProduct.Status.ACTIVE,
        )

        self._patch(
            self.store.id,
            self.product.id,
            {"status": "PAUSED"},
            self.owner_access,
        )

        other_assignment.refresh_from_db()
        self.assertEqual(other_assignment.status, self._StoreProduct.Status.ACTIVE)


class StoreProductDeleteEndpointTests(TestCase):
    """DELETE /api/v1/stores/{store_id}/products/{product_id}

    OWNER/MANAGER only. Hard-deletes the store_products row and cascades
    a hard-delete to store_inventories for all variants of that product
    in this store. The HQ-owned product_master and its variants are
    untouched.

    Per 156/157 policy: DISCONTINUED is orthogonal to store-side intent,
    so deleting a discontinued product is allowed (the typical "stock
    cleared, stop carrying" flow).
    """

    url_tmpl = "/api/v1/stores/{store_id}/products/{product_id}"

    def setUp(self):
        from products.models import (
            ProductMaster,
            ProductVariant,
            StoreInventory,
            StoreProduct,
        )

        self._json = json
        self._ProductMaster = ProductMaster
        self._ProductVariant = ProductVariant
        self._StoreInventory = StoreInventory
        self._StoreProduct = StoreProduct

        self.owner = User.objects.create_user(
            email="del_p_owner@example.com",
            password="X1!abcde",
            name="o",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="del_p_manager@example.com",
            password="X1!abcde",
            name="m",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="del_p_staff@example.com",
            password="X1!abcde",
            name="s",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="del_p_vmd@example.com",
            password="X1!abcde",
            name="v",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="del_p_out@example.com",
            password="X1!abcde",
            name="x",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="DP-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )

        # Target product with 2 variants and inventory in this store.
        self.product = ProductMaster.objects.create(
            user=self.outsider,
            name="Target",
            price=1000,
            width=10,
            height=10,
            depth=10,
        )
        self.v1 = ProductVariant.objects.create(
            product_master=self.product,
            size="S",
            sku_code="T-S",
        )
        self.v2 = ProductVariant.objects.create(
            product_master=self.product,
            size="M",
            sku_code="T-M",
        )
        self.assignment = StoreProduct.objects.create(
            store=self.store,
            product_master=self.product,
            status=StoreProduct.Status.ACTIVE,
        )
        StoreInventory.objects.create(
            store=self.store, variant=self.v1, stock_quantity=10
        )
        StoreInventory.objects.create(
            store=self.store, variant=self.v2, stock_quantity=5
        )

        # A second assigned product so total_count after delete is non-zero.
        self.bystander = ProductMaster.objects.create(
            user=self.outsider,
            name="Bystander",
            price=500,
            width=5,
            height=5,
            depth=5,
        )
        StoreProduct.objects.create(
            store=self.store,
            product_master=self.bystander,
            status=StoreProduct.Status.PAUSED,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _delete(self, store_id, product_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.delete(
            self.url_tmpl.format(store_id=store_id, product_id=product_id),
            **headers,
        )

    def test_success_owner_hard_deletes_assignment_and_inventory(self):
        response = self._delete(self.store.id, self.product.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(
            body["message"],
            "매장의 취급 상품 목록에서 성공적으로 삭제되었습니다.",
        )

        data = body["data"]
        self.assertEqual(data["deleted_product_id"], self.product.id)
        # bystander remains → total_count = 1
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(set(data.keys()), {"deleted_product_id", "total_count"})

        # Hard delete on store_products
        self.assertFalse(
            self._StoreProduct.objects.filter(
                store=self.store,
                product_master=self.product,
            ).exists()
        )
        # Inventory cascade — both variants' stock rows gone
        self.assertFalse(
            self._StoreInventory.objects.filter(
                store=self.store,
                variant__product_master=self.product,
            ).exists()
        )

    def test_product_master_and_variants_preserved(self):
        """HQ-owned data must be untouched — only the per-store assignment
        and inventory disappear."""
        self._delete(self.store.id, self.product.id, self.owner_access)

        # ProductMaster and its variants still alive
        self.assertTrue(
            self._ProductMaster.objects.filter(
                id=self.product.id, deleted_at__isnull=True
            ).exists()
        )
        self.assertEqual(
            self._ProductVariant.objects.filter(product_master=self.product).count(),
            2,
        )

    def test_manager_can_delete(self):
        response = self._delete(self.store.id, self.product.id, self.manager_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total_count"], 1)

    def test_staff_forbidden_with_spec_message(self):
        response = self._delete(self.store.id, self.product.id, self.staff_access)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")
        self.assertEqual(
            body["message"],
            "해당 매장의 정보를 수정할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
        )

        # Untouched
        self.assertTrue(
            self._StoreProduct.objects.filter(
                store=self.store,
                product_master=self.product,
            ).exists()
        )
        self.assertEqual(
            self._StoreInventory.objects.filter(
                store=self.store,
                variant__product_master=self.product,
            ).count(),
            2,
        )

    def test_vmd_can_delete(self):
        """VMD 도 ADMIN_ROLES 에 포함 — 매장 상품 삭제 가능."""
        response = self._delete(self.store.id, self.product.id, self.vmd_access)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            self._StoreProduct.objects.filter(
                store=self.store,
                product_master=self.product,
            ).exists()
        )

    def test_outsider_returns_store_not_found(self):
        """Same enumeration-prevention as the rest of the domain."""
        response = self._delete(self.store.id, self.product.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._delete(999_999, self.product.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._delete(self.store.id, self.product.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._delete(self.store.id, self.product.id)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_product_not_assigned_returns_404(self):
        unassigned = self._ProductMaster.objects.create(
            user=self.outsider,
            name="없는것",
            price=100,
            width=1,
            height=1,
            depth=1,
        )
        response = self._delete(self.store.id, unassigned.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "PRODUCT_NOT_ASSIGNED")
        self.assertEqual(body["message"], "해당 매장에 할당되어 있지 않은 상품입니다.")

    def test_nonexistent_product_returns_product_not_assigned(self):
        response = self._delete(self.store.id, 999_999, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_ASSIGNED")

    def test_double_delete_second_call_is_product_not_assigned(self):
        """After hard-delete, a second DELETE on the same path must read
        as 'not assigned' rather than succeeding silently."""
        self._delete(self.store.id, self.product.id, self.owner_access)
        response = self._delete(self.store.id, self.product.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_ASSIGNED")

    def test_discontinued_master_still_deletable(self):
        """156/157 policy: HQ-side discontinuation is orthogonal to
        store-side handling. A store running down stock must still be
        able to drop the assignment when the cleanup is done."""
        self.product.soft_delete()

        response = self._delete(self.store.id, self.product.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            self._StoreProduct.objects.filter(
                store=self.store,
                product_master=self.product,
            ).exists()
        )

    def test_other_stores_assignment_and_inventory_untouched(self):
        """Cross-store isolation: deleting in store A must not affect
        another store's assignment of the same product master."""
        other_owner = User.objects.create_user(
            email="del_p_other@example.com",
            password="X1!abcde",
            name="oo",
            confirmed=True,
        )
        other_store = Store.objects.create(
            user=other_owner,
            name="다른매장",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(
            store=other_store,
            user=other_owner,
            role=StoreMember.Role.OWNER,
        )
        other_assignment = self._StoreProduct.objects.create(
            store=other_store,
            product_master=self.product,
            status=self._StoreProduct.Status.ACTIVE,
        )
        other_inv = self._StoreInventory.objects.create(
            store=other_store,
            variant=self.v1,
            stock_quantity=999,
        )

        self._delete(self.store.id, self.product.id, self.owner_access)

        # Other store's assignment + inventory must survive
        self.assertTrue(
            self._StoreProduct.objects.filter(id=other_assignment.id).exists()
        )
        other_inv.refresh_from_db()
        self.assertEqual(other_inv.stock_quantity, 999)

    def test_total_count_reflects_remaining_after_delete(self):
        """Add a few more products, delete one, confirm count math."""
        extra1 = self._ProductMaster.objects.create(
            user=self.outsider,
            name="X1",
            price=100,
            width=1,
            height=1,
            depth=1,
        )
        extra2 = self._ProductMaster.objects.create(
            user=self.outsider,
            name="X2",
            price=100,
            width=1,
            height=1,
            depth=1,
        )
        self._StoreProduct.objects.create(store=self.store, product_master=extra1)
        self._StoreProduct.objects.create(
            store=self.store,
            product_master=extra2,
            status=self._StoreProduct.Status.PAUSED,
        )
        # store now has: target, bystander, extra1, extra2 → 4

        response = self._delete(self.store.id, self.product.id, self.owner_access)
        self.assertEqual(response.json()["data"]["total_count"], 3)

    def test_inventory_for_other_products_not_touched(self):
        """Cascade is scoped to the deleted product's variants only —
        inventory for unrelated products in the same store must survive."""
        other_p = self._ProductMaster.objects.create(
            user=self.outsider,
            name="other-stock",
            price=100,
            width=1,
            height=1,
            depth=1,
        )
        other_v = self._ProductVariant.objects.create(
            product_master=other_p,
            size="F",
            sku_code="OS-F",
        )
        self._StoreProduct.objects.create(store=self.store, product_master=other_p)
        survivor = self._StoreInventory.objects.create(
            store=self.store,
            variant=other_v,
            stock_quantity=42,
        )

        self._delete(self.store.id, self.product.id, self.owner_access)

        survivor.refresh_from_db()
        self.assertEqual(survivor.stock_quantity, 42)

    def test_inventory_cascade_includes_soft_deleted_variant(self):
        """Defensive: a discontinued variant's stock row must also be
        cleaned up. Otherwise the inventory book carries an orphan after
        the parent assignment is gone."""
        self.v1.soft_delete()  # discontinued variant
        # v1 still has its inventory row from setUp

        self._delete(self.store.id, self.product.id, self.owner_access)

        self.assertFalse(
            self._StoreInventory.objects.filter(
                store=self.store,
                variant=self.v1,
            ).exists()
        )


class StoreMembersListEndpointTests(TestCase):
    """GET /api/v1/stores/{store_id}/members

    Permission: any membership (read-only). Returns members + admin_quota.

    admin_quota.current = MANAGER + VICE_MANAGER + VMD count
    (decision: OWNER 는 본사라 매장 정원 무관, STAFF 는 admin tier 아님).
    Spec line 49 명세는 OWNER+MANAGER 였으나 role 체계 재정의로 정정.
    """

    url_tmpl = "/api/v1/stores/{store_id}/members"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="ml_owner@example.com",
            password="X1!abcde",
            name="본사김씨",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="ml_manager@example.com",
            password="X1!abcde",
            name="점장이씨",
            confirmed=True,
        )
        self.vice = User.objects.create_user(
            email="ml_vice@example.com",
            password="X1!abcde",
            name="부점장박씨",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="ml_vmd@example.com",
            password="X1!abcde",
            name="VMD최씨",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="ml_staff@example.com",
            password="X1!abcde",
            name="알바정씨",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="ml_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="ML-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
            max_admin_count=5,
        )
        # 의도적으로 created_at 순서를 강제하기 위해 sequential 생성
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.vice, role=StoreMember.Role.VICE_MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.vice_access = str(AccessToken.for_user(self.vice))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _get(self, store_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url_tmpl.format(store_id=store_id), **headers)

    def test_success_owner_returns_full_payload(self):
        response = self._get(self.store.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "매장 직원 목록 조회에 성공했습니다.")

        data = body["data"]
        self.assertEqual(set(data.keys()), {"admin_quota", "members"})
        self.assertEqual(set(data["admin_quota"].keys()), {"current", "max"})
        # MANAGER + VICE_MANAGER + VMD = 3 (OWNER, STAFF 제외)
        self.assertEqual(data["admin_quota"]["current"], 3)
        self.assertEqual(data["admin_quota"]["max"], 5)

        self.assertEqual(len(data["members"]), 5)
        # Lock projection per member
        self.assertEqual(
            set(data["members"][0].keys()),
            {
                "user_id",
                "name",
                "email",
                "profile_image_url",
                "role",
                "joined_at",
            },
        )

    def test_admin_quota_excludes_owner_and_staff(self):
        """OWNER 는 본사 (매장 정원 외), STAFF 는 admin tier 아님 — 둘 다 카운트 제외."""
        # 매장에 OWNER 1 + STAFF 1 만 두고 검증
        bare_store = Store.objects.create(
            user=self.owner,
            name="bare",
            address="X",
            width=1,
            height=1,
            depth=1,
            max_admin_count=5,
        )
        StoreMember.objects.create(
            store=bare_store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=bare_store, user=self.staff, role=StoreMember.Role.STAFF
        )

        response = self._get(bare_store.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["admin_quota"]["current"], 0)

    def test_admin_quota_counts_manager_vice_vmd(self):
        """admin_quota 는 MANAGER + VICE_MANAGER + VMD 합산."""
        # setUp 의 store 는 이미 5명 (OWNER/MANAGER/VICE/VMD/STAFF)
        response = self._get(self.store.id, self.owner_access)
        # MANAGER 1 + VICE_MANAGER 1 + VMD 1 = 3
        self.assertEqual(response.json()["data"]["admin_quota"]["current"], 3)

    def test_admin_quota_max_reflects_store_max_admin_count(self):
        """max 는 stores.max_admin_count 그대로 echo (default 5)."""
        self.store.max_admin_count = 7
        self.store.save(update_fields=["max_admin_count"])

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.json()["data"]["admin_quota"]["max"], 7)

    def test_members_ordered_by_joined_at(self):
        """created_at(=joined_at) 오름차순. setUp 에서 OWNER → MANAGER →
        VICE → VMD → STAFF 순으로 만들었으므로 그대로 나와야 함."""
        response = self._get(self.store.id, self.owner_access)
        roles = [m["role"] for m in response.json()["data"]["members"]]
        self.assertEqual(
            roles,
            ["OWNER", "MANAGER", "VICE_MANAGER", "VMD", "STAFF"],
        )

    def test_member_payload_fields(self):
        response = self._get(self.store.id, self.owner_access)
        first = response.json()["data"]["members"][0]
        self.assertEqual(first["user_id"], self.owner.id)
        self.assertEqual(first["name"], "본사김씨")
        self.assertEqual(first["email"], self.owner.email)
        self.assertIsNone(first["profile_image_url"])
        self.assertEqual(first["role"], "OWNER")
        self.assertIn("joined_at", first)

    def test_staff_can_view_members(self):
        """플레인 멤버십(STAFF 포함) 누구나 조회 가능 — read-only."""
        response = self._get(self.store.id, self.staff_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]["members"]), 5)

    def test_vice_manager_can_view(self):
        response = self._get(self.store.id, self.vice_access)
        self.assertEqual(response.status_code, 200)

    def test_vmd_can_view(self):
        response = self._get(self.store.id, self.vmd_access)
        self.assertEqual(response.status_code, 200)

    def test_outsider_returns_store_not_found(self):
        """비회원은 매장 존재 자체 미노출 (enumeration prevention)."""
        response = self._get(self.store.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._get(999_999, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._get(self.store.id)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_other_stores_members_not_leaked(self):
        """매장 격리: 다른 매장 멤버는 응답에 안 들어감."""
        other_store = Store.objects.create(
            user=self.outsider,
            name="다른매장",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(
            store=other_store,
            user=self.outsider,
            role=StoreMember.Role.OWNER,
        )
        intruder = User.objects.create_user(
            email="ml_intruder@example.com",
            password="X1!abcde",
            name="틈입자",
            confirmed=True,
        )
        StoreMember.objects.create(
            store=other_store,
            user=intruder,
            role=StoreMember.Role.MANAGER,
        )

        response = self._get(self.store.id, self.owner_access)
        emails = {m["email"] for m in response.json()["data"]["members"]}
        self.assertNotIn("ml_intruder@example.com", emails)
        self.assertEqual(len(response.json()["data"]["members"]), 5)


class StoreMemberRoleUpdateEndpointTests(TestCase):
    """PATCH /api/v1/stores/{store_id}/members/{user_id}

    ADMIN_ROLES gate (STAFF 만 거부) + spec 코드 FORBIDDEN_ACTION (다른
    엔드포인트의 FORBIDDEN_ACCESS 와 다름).

    OWNER 의 role 변경 차단 (소유권 이관 advanced).
    OWNER 임명 차단 (schema 단계, MVP 범위 밖).
    Quota 검증: STAFF → STORE_QUOTA_ROLES 승급 시 max_admin_count 와 비교.
    """

    url_tmpl = "/api/v1/stores/{store_id}/members/{user_id}"

    def setUp(self):
        self._json = json
        self.owner = User.objects.create_user(
            email="mr_owner@example.com",
            password="X1!abcde",
            name="본사",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="mr_mgr@example.com",
            password="X1!abcde",
            name="점장",
            confirmed=True,
        )
        self.vice = User.objects.create_user(
            email="mr_vice@example.com",
            password="X1!abcde",
            name="부점장",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="mr_vmd@example.com",
            password="X1!abcde",
            name="VMD",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="mr_staff@example.com",
            password="X1!abcde",
            name="알바",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="mr_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="MR-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
            max_admin_count=5,
        )
        # 멤버 5명 — admin tier (MANAGER+VICE+VMD) = 3명, max=5 이라 quota 여유 2명
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        self.mgr_membership = StoreMember.objects.create(
            store=self.store,
            user=self.manager,
            role=StoreMember.Role.MANAGER,
        )
        self.vice_membership = StoreMember.objects.create(
            store=self.store,
            user=self.vice,
            role=StoreMember.Role.VICE_MANAGER,
        )
        self.vmd_membership = StoreMember.objects.create(
            store=self.store,
            user=self.vmd,
            role=StoreMember.Role.VMD,
        )
        self.staff_membership = StoreMember.objects.create(
            store=self.store,
            user=self.staff,
            role=StoreMember.Role.STAFF,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.vice_access = str(AccessToken.for_user(self.vice))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _patch(self, store_id, user_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            self.url_tmpl.format(store_id=store_id, user_id=user_id),
            data=self._json.dumps(body),
            content_type="application/json",
            **headers,
        )

    # ── Success cases ──────────────────────────────────────────

    def test_success_owner_promotes_staff_to_manager(self):
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "MANAGER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "직원 권한이 성공적으로 변경되었습니다.")

        data = body["data"]
        self.assertEqual(data["store_id"], self.store.id)
        self.assertEqual(data["user_id"], self.staff.id)
        self.assertEqual(data["role"], "MANAGER")
        self.assertIn("updated_at", data)
        self.assertEqual(
            set(data.keys()), {"store_id", "user_id", "role", "updated_at"}
        )

        self.staff_membership.refresh_from_db()
        self.assertEqual(self.staff_membership.role, StoreMember.Role.MANAGER)

    def test_success_promote_to_vice_manager(self):
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VICE_MANAGER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.staff_membership.refresh_from_db()
        self.assertEqual(self.staff_membership.role, StoreMember.Role.VICE_MANAGER)

    def test_success_demote_to_staff(self):
        response = self._patch(
            self.store.id,
            self.manager.id,
            {"role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.mgr_membership.refresh_from_db()
        self.assertEqual(self.mgr_membership.role, StoreMember.Role.STAFF)

    def test_success_lateral_within_quota(self):
        """MANAGER → VICE_MANAGER (둘 다 STORE_QUOTA_ROLES) 는 quota 변동 없음."""
        response = self._patch(
            self.store.id,
            self.manager.id,
            {"role": "VICE_MANAGER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

    def test_success_manager_can_update(self):
        """159 의 ADMIN_ROLES 정책 일관 — MANAGER 도 가능."""
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.manager_access,
        )
        self.assertEqual(response.status_code, 200)

    def test_success_vmd_can_update(self):
        """159 의 ADMIN_ROLES 정책 일관 — VMD 도 가능."""
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.vmd_access,
        )
        self.assertEqual(response.status_code, 200)

    # ── No-op ──────────────────────────────────────────────────

    def test_no_op_same_role_does_not_bump_updated_at(self):
        """현재 == 요청 인 경우 save 스킵 (157 와 동일 정책)."""
        self.mgr_membership.refresh_from_db()
        before = self.mgr_membership.updated_at

        response = self._patch(
            self.store.id,
            self.manager.id,
            {"role": "MANAGER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["role"], "MANAGER")

        self.mgr_membership.refresh_from_db()
        self.assertEqual(self.mgr_membership.updated_at, before)

    # ── Permission ─────────────────────────────────────────────

    def test_staff_forbidden_with_spec_code_and_message(self):
        """스펙 line 97 에러 — code = FORBIDDEN_ACTION (FORBIDDEN_ACCESS 아님)."""
        response = self._patch(
            self.store.id,
            self.vmd.id,
            {"role": "STAFF"},
            self.staff_access,
        )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN_ACTION")
        self.assertEqual(
            body["message"],
            "직원의 권한을 변경할 수 있는 관리자 권한이 없습니다.",
        )

    def test_outsider_returns_store_not_found(self):
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.outsider_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._patch(self.store.id, self.staff.id, {"role": "VMD"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    # ── OWNER protection ───────────────────────────────────────

    def test_owner_role_cannot_be_changed(self):
        """OWNER 권한은 소유권 이관 의미라 별도 endpoint 필요. 변경 거부."""
        response = self._patch(
            self.store.id,
            self.owner.id,
            {"role": "MANAGER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "FORBIDDEN_ACTION")
        self.assertIn("OWNER", body["message"])

        # OWNER membership 그대로
        owner_m = StoreMember.objects.get(store=self.store, user=self.owner)
        self.assertEqual(owner_m.role, StoreMember.Role.OWNER)

    def test_owner_value_in_body_rejected_by_schema(self):
        """OWNER 임명 시도 → schema Literal 단계에서 INVALID_PARAMETER."""
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "OWNER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    # ── Schema validation ──────────────────────────────────────

    def test_invalid_role_value_rejected(self):
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "FOO"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    # ── Membership ─────────────────────────────────────────────

    def test_member_not_found_for_non_member_user(self):
        """스토어 멤버 아닌 user_id 로 PATCH → 404 MEMBER_NOT_FOUND."""
        response = self._patch(
            self.store.id,
            self.outsider.id,
            {"role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "MEMBER_NOT_FOUND")
        self.assertEqual(body["message"], "해당 매장의 직원이 아닙니다.")

    def test_member_not_found_for_nonexistent_user_id(self):
        response = self._patch(
            self.store.id,
            999_999,
            {"role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "MEMBER_NOT_FOUND")

    # ── Store boundary ─────────────────────────────────────────

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._patch(
            999_999,
            self.staff.id,
            {"role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    # ── Quota ──────────────────────────────────────────────────

    def test_quota_exceeded_when_promoting_beyond_max(self):
        """max_admin_count=3 환경에서 admin tier 가 이미 3명일 때 STAFF→VMD 승급 차단."""
        self.store.max_admin_count = 3
        self.store.save(update_fields=["max_admin_count"])
        # 현재 admin tier: MANAGER+VICE+VMD = 3. STAFF→VMD 시 4 → max 초과.

        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "ADMIN_QUOTA_EXCEEDED")
        self.assertIn("3", body["message"])

        # Membership 미변경
        self.staff_membership.refresh_from_db()
        self.assertEqual(self.staff_membership.role, StoreMember.Role.STAFF)

    def test_quota_pass_when_below_max(self):
        """admin tier 3명, max=5 → STAFF→VMD 승급 시 4명 → OK."""
        response = self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

    def test_lateral_within_quota_skips_check(self):
        """MANAGER→VICE_MANAGER 는 max 초과여도 OK (count 변동 없음)."""
        self.store.max_admin_count = 1  # admin tier 가 이미 max 보다 큼 (=3)
        self.store.save(update_fields=["max_admin_count"])

        response = self._patch(
            self.store.id,
            self.manager.id,
            {"role": "VICE_MANAGER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

    def test_demotion_skips_quota_check(self):
        """MANAGER→STAFF 강등은 max 초과여도 OK (count 감소)."""
        self.store.max_admin_count = 1
        self.store.save(update_fields=["max_admin_count"])

        response = self._patch(
            self.store.id,
            self.manager.id,
            {"role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.mgr_membership.refresh_from_db()
        self.assertEqual(self.mgr_membership.role, StoreMember.Role.STAFF)

    def test_self_modification_demote_self(self):
        """admin 이 자기 자신 강등 가능 (스펙 금지 없음)."""
        response = self._patch(
            self.store.id,
            self.manager.id,
            {"role": "STAFF"},
            self.manager_access,
        )
        self.assertEqual(response.status_code, 200)
        self.mgr_membership.refresh_from_db()
        self.assertEqual(self.mgr_membership.role, StoreMember.Role.STAFF)

    def test_other_stores_membership_not_touched(self):
        """매장 격리 — 같은 user 가 다른 매장 멤버여도 다른 매장의 row 는 무관."""
        other_store = Store.objects.create(
            user=self.outsider,
            name="다른매장",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(
            store=other_store,
            user=self.outsider,
            role=StoreMember.Role.OWNER,
        )
        # staff 가 다른 매장 에서는 MANAGER
        other_staff_m = StoreMember.objects.create(
            store=other_store,
            user=self.staff,
            role=StoreMember.Role.MANAGER,
        )

        # 우리 매장 staff 의 role 만 VMD 로 변경
        self._patch(
            self.store.id,
            self.staff.id,
            {"role": "VMD"},
            self.owner_access,
        )

        other_staff_m.refresh_from_db()
        self.assertEqual(other_staff_m.role, StoreMember.Role.MANAGER)


class StoreInvitationCreateEndpointTests(TestCase):
    """POST /api/v1/stores/{store_id}/invitations

    ADMIN_ROLES 게이트 (스펙 코드 FORBIDDEN_ACCESS).
    검증 순서: 권한 → ALREADY_MEMBER → ALREADY_INVITED → editor quota.
    Editor quota = 현재 admin tier 멤버 + 같은 tier 의 alive pending invitation.
    Celery 태스크는 transaction.on_commit 으로 디스패치 (DB row 보장 후 발송).
    """

    url_tmpl = "/api/v1/stores/{store_id}/invitations"

    def setUp(self):
        from unittest.mock import patch
        from stores.models import StoreInvitation

        self._json = json
        self._patch_target = patch
        self._StoreInvitation = StoreInvitation

        self.owner = User.objects.create_user(
            email="iv_owner@example.com",
            password="X1!abcde",
            name="본사",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="iv_mgr@example.com",
            password="X1!abcde",
            name="점장",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="iv_staff@example.com",
            password="X1!abcde",
            name="알바",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="iv_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        # 이미 매장 멤버인 사용자 — ALREADY_MEMBER 시나리오용
        self.existing = User.objects.create_user(
            email="iv_existing@example.com",
            password="X1!abcde",
            name="기존",
            confirmed=True,
        )

        self.store = Store.objects.create(
            user=self.owner,
            name="IV-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
            max_admin_count=5,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )
        StoreMember.objects.create(
            store=self.store, user=self.existing, role=StoreMember.Role.VMD
        )
        # admin tier 현재: MANAGER + VMD = 2명, max=5 → 여유 3

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _post(self, store_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url_tmpl.format(store_id=store_id),
            data=self._json.dumps(body),
            content_type="application/json",
            **headers,
        )

    # ── Success ────────────────────────────────────────────────

    def test_success_owner_creates_invitation_dispatches_email(self):
        with self._patch_target(
            "stores.services.send_store_invitation_email.delay"
        ) as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "newbie@example.com", "target_role": "VMD"},
                    self.owner_access,
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "초대장이 성공적으로 발송되었습니다.")

        data = body["data"]
        self.assertEqual(
            set(data.keys()),
            {
                "invitation_id",
                "invitee_email",
                "target_role",
                "invite_link",
                "expires_at",
            },
        )
        self.assertEqual(data["invitee_email"], "newbie@example.com")
        self.assertEqual(data["target_role"], "VMD")
        self.assertIn("/invite?token=", data["invite_link"])

        # DB row 생성 확인
        inv = self._StoreInvitation.objects.get(id=data["invitation_id"])
        self.assertEqual(inv.invitee_email, "newbie@example.com")
        self.assertEqual(inv.inviter, self.owner)
        self.assertFalse(inv.is_used)
        self.assertGreater(inv.expires_at, timezone.now() + timedelta(hours=23))
        self.assertLess(inv.expires_at, timezone.now() + timedelta(hours=25))
        self.assertGreater(len(inv.invite_token), 30)

        # Celery 태스크 호출 (on_commit 후)
        mock_delay.assert_called_once()
        call_kwargs = mock_delay.call_args.kwargs
        self.assertEqual(call_kwargs["invitee_email"], "newbie@example.com")
        self.assertEqual(call_kwargs["store_name"], "IV-base")
        self.assertEqual(call_kwargs["inviter_name"], "본사")

    def test_success_manager_can_invite(self):
        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "x@example.com", "target_role": "STAFF"},
                    self.manager_access,
                )
        self.assertEqual(response.status_code, 200)

    def test_success_invite_unregistered_user(self):
        """User row 가 없는 이메일도 초대 가능 — 워크스페이스 정상 시나리오."""
        self.assertFalse(
            User.objects.filter(email="never_signed_up@example.com").exists()
        )
        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {
                        "invite_email": "never_signed_up@example.com",
                        "target_role": "STAFF",
                    },
                    self.owner_access,
                )
        self.assertEqual(response.status_code, 200)

    def test_success_staff_invite_skips_quota_check(self):
        """STAFF 초대는 STORE_QUOTA_ROLES 밖 — quota 체크 안 함."""
        # max=1 으로 줄여도 STAFF 초대는 통과해야 함
        self.store.max_admin_count = 1
        self.store.save(update_fields=["max_admin_count"])

        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "newstaff@example.com", "target_role": "STAFF"},
                    self.owner_access,
                )
        self.assertEqual(response.status_code, 200)

    # ── Permission ─────────────────────────────────────────────

    def test_staff_forbidden(self):
        response = self._post(
            self.store.id,
            {"invite_email": "x@example.com", "target_role": "STAFF"},
            self.staff_access,
        )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")
        self.assertIn("OWNER/MANAGER", body["message"])

    def test_outsider_returns_store_not_found(self):
        response = self._post(
            self.store.id,
            {"invite_email": "x@example.com", "target_role": "STAFF"},
            self.outsider_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_unauthorized_without_token(self):
        response = self._post(
            self.store.id,
            {"invite_email": "x@example.com", "target_role": "STAFF"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    # ── Already member / invited ───────────────────────────────

    def test_already_member_rejected(self):
        response = self._post(
            self.store.id,
            {"invite_email": self.existing.email, "target_role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "ALREADY_MEMBER")
        self.assertEqual(
            body["message"], "해당 이메일 사용자는 이미 이 매장의 멤버입니다."
        )

    def test_already_invited_rejected(self):
        """같은 store + 같은 email + alive pending invitation → 거부."""
        self._StoreInvitation.objects.create(
            store=self.store,
            inviter=self.owner,
            invitee_email="dup@example.com",
            invite_token="dup-token-aaaa",
            target_role=StoreMember.Role.STAFF,
            expires_at=timezone.now() + timedelta(hours=12),
        )
        response = self._post(
            self.store.id,
            {"invite_email": "dup@example.com", "target_role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "ALREADY_INVITED")

    def test_expired_invitation_does_not_block_reinvite(self):
        """만료된 초대장은 ALREADY_INVITED 트리거 안 함 — 재초대 가능."""
        self._StoreInvitation.objects.create(
            store=self.store,
            inviter=self.owner,
            invitee_email="expired@example.com",
            invite_token="expired-token-aaaa",
            target_role=StoreMember.Role.STAFF,
            expires_at=timezone.now() - timedelta(hours=1),  # 만료됨
        )
        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "expired@example.com", "target_role": "STAFF"},
                    self.owner_access,
                )
        self.assertEqual(response.status_code, 200)

    def test_used_invitation_does_not_block_reinvite(self):
        """이미 used 처리된 초대장도 ALREADY_INVITED 미해당."""
        self._StoreInvitation.objects.create(
            store=self.store,
            inviter=self.owner,
            invitee_email="used@example.com",
            invite_token="used-token-aaaa",
            target_role=StoreMember.Role.STAFF,
            expires_at=timezone.now() + timedelta(hours=12),
            is_used=True,
        )
        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "used@example.com", "target_role": "STAFF"},
                    self.owner_access,
                )
        self.assertEqual(response.status_code, 200)

    # ── Editor quota ───────────────────────────────────────────

    def test_editor_quota_exceeded_by_pending_invitations(self):
        """admin tier 2 + pending 3 → 5 = max=5. 새로 VMD 초대 시 +1=6 → 차단."""
        for i in range(3):
            self._StoreInvitation.objects.create(
                store=self.store,
                inviter=self.owner,
                invitee_email=f"pending{i}@example.com",
                invite_token=f"tk-{i:04d}-xxxxxxxxxxxxxxxxxxxxx",
                target_role=StoreMember.Role.MANAGER,
                expires_at=timezone.now() + timedelta(hours=12),
            )

        response = self._post(
            self.store.id,
            {"invite_email": "newvmd@example.com", "target_role": "VMD"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "EDITOR_QUOTA_EXCEEDED")
        self.assertIn("5", body["message"])

    def test_editor_quota_pass_when_below_max(self):
        """admin tier 2, pending 0 → max=5 여유 3. VMD 초대 OK."""
        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "newvmd@example.com", "target_role": "VMD"},
                    self.owner_access,
                )
        self.assertEqual(response.status_code, 200)

    def test_editor_quota_ignores_other_stores_pending(self):
        """매장 격리 — 다른 매장의 pending invitation 은 카운트 안 됨."""
        other_owner = User.objects.create_user(
            email="iv_otherowner@example.com",
            password="X1!abcde",
            name="o",
            confirmed=True,
        )
        other_store = Store.objects.create(
            user=other_owner,
            name="other",
            address="X",
            width=1,
            height=1,
            depth=1,
            max_admin_count=5,
        )
        StoreMember.objects.create(
            store=other_store,
            user=other_owner,
            role=StoreMember.Role.OWNER,
        )
        for i in range(10):
            self._StoreInvitation.objects.create(
                store=other_store,
                inviter=other_owner,
                invitee_email=f"other_pending{i}@example.com",
                invite_token=f"otk-{i:04d}-xxxxxxxxxxxxxxxxxxx",
                target_role=StoreMember.Role.MANAGER,
                expires_at=timezone.now() + timedelta(hours=12),
            )

        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(
                    self.store.id,
                    {"invite_email": "vmd@example.com", "target_role": "VMD"},
                    self.owner_access,
                )
        self.assertEqual(response.status_code, 200)

    # ── Schema validation ──────────────────────────────────────

    def test_invalid_target_role_rejected(self):
        response = self._post(
            self.store.id,
            {"invite_email": "x@example.com", "target_role": "FOO"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    def test_owner_value_rejected_at_schema(self):
        """OWNER 임명 차단 (160 과 동일 정책)."""
        response = self._post(
            self.store.id,
            {"invite_email": "x@example.com", "target_role": "OWNER"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    def test_invalid_email_rejected(self):
        response = self._post(
            self.store.id,
            {"invite_email": "not-an-email", "target_role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    # ── Store boundary ─────────────────────────────────────────

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._post(
            999_999,
            {"invite_email": "x@example.com", "target_role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])
        response = self._post(
            self.store.id,
            {"invite_email": "x@example.com", "target_role": "STAFF"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    # ── Token / link format ────────────────────────────────────

    def test_token_unique_across_invitations(self):
        """secrets.token_urlsafe(32) 충돌 가능성 ≈ 0 검증 (smoke)."""
        with self._patch_target("stores.services.send_store_invitation_email.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                self._post(
                    self.store.id,
                    {"invite_email": "a@example.com", "target_role": "STAFF"},
                    self.owner_access,
                )
                self._post(
                    self.store.id,
                    {"invite_email": "b@example.com", "target_role": "STAFF"},
                    self.owner_access,
                )

        tokens = list(
            self._StoreInvitation.objects.values_list("invite_token", flat=True)
        )
        self.assertEqual(len(tokens), len(set(tokens)))


class InvitationAcceptEndpointTests(TestCase):
    """POST /api/v1/invitations/accept

    URL prefix 가 /stores 가 아니라 /invitations 라 별도 router 마운트.
    코드는 stores 앱 안에 colocate (StoreInvitation 모델·서비스·테스트 모음).

    검증 순서 (보안 우선): 토큰 미존재 → is_used → expired → email mismatch
    → store soft-deleted → already member → 통과.
    토큰 존재성 노출 안 하기 위해 mismatch/store-deleted 도 INVALID_TOKEN 로 묶음.
    """

    url = "/api/v1/invitations/accept"

    def setUp(self):
        from stores.models import StoreInvitation

        self._json = json
        self._StoreInvitation = StoreInvitation

        self.owner = User.objects.create_user(
            email="ac_owner@example.com",
            password="X1!abcde",
            name="본사",
            confirmed=True,
        )
        self.invitee = User.objects.create_user(
            email="ac_invitee@example.com",
            password="X1!abcde",
            name="신입",
            confirmed=True,
        )
        self.other = User.objects.create_user(
            email="ac_other@example.com",
            password="X1!abcde",
            name="다른사람",
            confirmed=True,
        )

        self.store = Store.objects.create(
            user=self.owner,
            name="AC-base",
            address="서울",
            width=100,
            height=100,
            depth=100,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )

        # alive invitation — invitee 용 VMD 초대
        self.invitation = self._StoreInvitation.objects.create(
            store=self.store,
            inviter=self.owner,
            invitee_email=self.invitee.email,
            invite_token="alive-token-aaaaaaaaaaaaaaaaaaaa",
            target_role=StoreMember.Role.VMD,
            expires_at=timezone.now() + timedelta(hours=12),
        )

        self.invitee_access = str(AccessToken.for_user(self.invitee))
        self.other_access = str(AccessToken.for_user(self.other))
        self.owner_access = str(AccessToken.for_user(self.owner))

    def _post(self, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url,
            data=self._json.dumps(body),
            content_type="application/json",
            **headers,
        )

    # ── Success ────────────────────────────────────────────────

    def test_success_creates_member_and_marks_token_used(self):
        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(
            body["message"], "매장 워크스페이스에 성공적으로 합류했습니다."
        )

        data = body["data"]
        self.assertEqual(data["store_id"], self.store.id)
        self.assertEqual(data["store_name"], "AC-base")
        self.assertEqual(data["granted_role"], "VMD")
        self.assertIn("joined_at", data)
        self.assertEqual(
            set(data.keys()),
            {
                "store_id",
                "store_name",
                "granted_role",
                "joined_at",
            },
        )

        # store_member 생성됨
        member = StoreMember.objects.get(store=self.store, user=self.invitee)
        self.assertEqual(member.role, StoreMember.Role.VMD)

        # token used 마킹
        self.invitation.refresh_from_db()
        self.assertTrue(self.invitation.is_used)

    # ── Validation: 1. 토큰 미존재 ────────────────────────────

    def test_invalid_token_when_token_does_not_exist(self):
        response = self._post(
            {"invite_token": "this-token-does-not-exist"},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "INVALID_TOKEN")
        self.assertEqual(
            body["message"], "유효하지 않거나 이미 사용된 초대 토큰입니다."
        )

    # ── Validation: 2. is_used ────────────────────────────────

    def test_invalid_token_when_already_used(self):
        self.invitation.is_used = True
        self.invitation.save(update_fields=["is_used"])

        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "INVALID_TOKEN")

    # ── Validation: 3. expired ────────────────────────────────

    def test_expired_invitation_returns_invitation_expired(self):
        self.invitation.expires_at = timezone.now() - timedelta(hours=1)
        self.invitation.save(update_fields=["expires_at"])

        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 410)
        body = response.json()
        self.assertEqual(body["code"], "INVITATION_EXPIRED")
        self.assertEqual(
            body["message"],
            "초대 유효 기간이 만료되었습니다. 관리자에게 재발송을 요청하세요.",
        )

    # ── Validation: 4. email mismatch ─────────────────────────

    def test_email_mismatch_treated_as_invalid_token(self):
        """다른 사람 (other) 이 invitee 용 토큰으로 accept 시도 → 보안상 INVALID_TOKEN."""
        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.other_access,
        )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        # mismatch 라고 알려주지 않음 — 토큰 존재성 비밀
        self.assertEqual(body["code"], "INVALID_TOKEN")
        self.assertEqual(
            body["message"], "유효하지 않거나 이미 사용된 초대 토큰입니다."
        )

        # 토큰 used 안 됐는지, member 생성 안 됐는지
        self.invitation.refresh_from_db()
        self.assertFalse(self.invitation.is_used)
        self.assertFalse(
            StoreMember.objects.filter(store=self.store, user=self.other).exists()
        )

    def test_email_match_is_case_insensitive(self):
        """이메일 비교는 대소문자 무관 (RFC 5321 — local part 도 통상 case-insensitive 운영)."""
        self.invitation.invitee_email = self.invitee.email.upper()
        self.invitation.save(update_fields=["invitee_email"])

        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 200)

    # ── Validation: 5. soft-deleted store ─────────────────────

    def test_soft_deleted_store_treated_as_invalid_token(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 404)
        # store 존재성 노출 안 함 → INVALID_TOKEN 로 묶음
        self.assertEqual(response.json()["code"], "INVALID_TOKEN")

    # ── Validation: 6. already member ─────────────────────────

    def test_already_member_returns_409(self):
        """invitee 가 다른 경로로 이미 멤버인 상태에서 accept 시도."""
        StoreMember.objects.create(
            store=self.store,
            user=self.invitee,
            role=StoreMember.Role.STAFF,
        )

        response = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "ALREADY_MEMBER")
        self.assertEqual(body["message"], "이미 이 매장의 멤버입니다.")

        # 기존 멤버십 그대로 (덮어쓰기 안 함, 토큰 미사용)
        member = StoreMember.objects.get(store=self.store, user=self.invitee)
        self.assertEqual(member.role, StoreMember.Role.STAFF)
        self.invitation.refresh_from_db()
        self.assertFalse(self.invitation.is_used)

    # ── Auth ───────────────────────────────────────────────────

    def test_unauthorized_without_token(self):
        response = self._post({"invite_token": self.invitation.invite_token})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    # ── Schema ─────────────────────────────────────────────────

    def test_missing_invite_token_rejected(self):
        response = self._post({}, self.invitee_access)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    # ── Idempotency ────────────────────────────────────────────

    def test_second_accept_with_same_token_blocked(self):
        """첫 accept 성공 → 토큰 used 마킹 → 재요청은 INVALID_TOKEN."""
        first = self._post(
            {"invite_token": self.invitation.invite_token},
            self.invitee_access,
        )
        self.assertEqual(first.status_code, 200)

        # 같은 토큰 재시도 (다른 사람이 가로챘다고 가정)
        second = self._post(
            {"invite_token": self.invitation.invite_token},
            self.other_access,
        )
        self.assertEqual(second.status_code, 404)
        self.assertEqual(second.json()["code"], "INVALID_TOKEN")

    # ── Cross-store isolation ──────────────────────────────────

    def test_invitation_for_other_store_grants_membership_in_correct_store(self):
        """invitee 가 두 매장 invitation 가지고 있을 때, 토큰 별로 정확한 매장 합류."""
        other_store = Store.objects.create(
            user=self.owner,
            name="other-store",
            address="X",
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(
            store=other_store, user=self.owner, role=StoreMember.Role.OWNER
        )
        other_inv = self._StoreInvitation.objects.create(
            store=other_store,
            inviter=self.owner,
            invitee_email=self.invitee.email,
            invite_token="other-store-token-bbbbbbbbbb",
            target_role=StoreMember.Role.MANAGER,
            expires_at=timezone.now() + timedelta(hours=12),
        )

        response = self._post(
            {"invite_token": other_inv.invite_token},
            self.invitee_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["store_id"], other_store.id)

        # other_store 에만 멤버 생성, self.store 에는 안 됨
        self.assertTrue(
            StoreMember.objects.filter(store=other_store, user=self.invitee).exists()
        )
        self.assertFalse(
            StoreMember.objects.filter(store=self.store, user=self.invitee).exists()
        )


class StoreCreateEndpointTests(TestCase):
    """POST /api/v1/users/me/stores — 매장 등록 + 가상 공간 생성.

    Pattern B: URL 은 users 도메인 아래지만, Store/StoreMember/StoreImage 모두
    stores 도메인 모델이라 로직은 stores.services.create_store 에 위치.
    테스트도 stores/tests.py 에 colocate.

    핵심 보장:
      - 매장 생성과 동시에 요청자가 MANAGER(점장) 멤버십을 가짐 (이후 모든
        멤버십 기반 조회의 진입점). 본사/점장 분리 정책이 다시 도입되면 OWNER
        로 되돌릴 수 있도록 enum 은 보존.
      - floorplan_image_url, actual_photo_url 이 들어오면 같은 트랜잭션 안에서
        StoreImage row 생성. 누락이면 row 자체가 생성되지 않음.
      - 응답 필드는 spec 에 명시된 7 키 (store_id/name/address/width/height/depth/
        created_at) 만 포함.
    """

    url = "/api/v1/users/me/stores"

    def setUp(self):
        self.user = User.objects.create_user(
            email="creator@example.com",
            password="X1!abcde",
            name="생성자",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

    def _post(self, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def _valid_body(self, **overrides):
        body = {
            "name": "DO-GO 성수 팝업",
            "address": "서울 강남구 강남대로 446 한일빌딩",
            "width": 6000,
            "height": 3500,
            "depth": 5000,
        }
        body.update(overrides)
        return body

    def test_success_creates_store_and_manager_membership(self):
        response = self._post(self._valid_body(), self.access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(
            body["message"],
            "신규 매장 가상 공간이 성공적으로 생성되었습니다.",
        )

        data = body["data"]
        self.assertEqual(
            set(data.keys()),
            {"store_id", "name", "address", "width", "height", "depth", "created_at"},
        )
        self.assertEqual(data["name"], "DO-GO 성수 팝업")
        self.assertEqual(data["address"], "서울 강남구 강남대로 446 한일빌딩")
        self.assertEqual(data["width"], 6000)
        self.assertEqual(data["height"], 3500)
        self.assertEqual(data["depth"], 5000)

        # DB rows: Store + MANAGER 멤버십이 함께 생성됐는지
        store = Store.objects.get(id=data["store_id"])
        self.assertEqual(store.user_id, self.user.id)
        self.assertEqual(store.name, "DO-GO 성수 팝업")

        membership = StoreMember.objects.get(store=store, user=self.user)
        self.assertEqual(membership.role, StoreMember.Role.MANAGER)

        # 이미지 미전달 → StoreImage row 없음
        self.assertFalse(StoreImage.objects.filter(store=store).exists())

    def test_success_with_both_image_urls_creates_two_image_rows(self):
        body = self._valid_body(
            floorplan_image_url="https://cdn.example.com/stores/images/floor.png",
            actual_photo_url="https://cdn.example.com/stores/images/photo.jpg",
        )
        response = self._post(body, self.access)
        self.assertEqual(response.status_code, 200)

        store = Store.objects.get(id=response.json()["data"]["store_id"])
        floor = StoreImage.objects.get(
            store=store,
            image_type=StoreImage.ImageType.FLOORPLAN,
        )
        actual = StoreImage.objects.get(
            store=store,
            image_type=StoreImage.ImageType.ACTUAL_PHOTO,
        )
        self.assertTrue(floor.image_url.name.endswith("floor.png"))
        self.assertTrue(actual.image_url.name.endswith("photo.jpg"))

    def test_success_with_only_floorplan(self):
        body = self._valid_body(
            floorplan_image_url="https://cdn.example.com/stores/images/floor.png"
        )
        response = self._post(body, self.access)
        self.assertEqual(response.status_code, 200)

        store = Store.objects.get(id=response.json()["data"]["store_id"])
        self.assertEqual(
            StoreImage.objects.filter(
                store=store,
                image_type=StoreImage.ImageType.FLOORPLAN,
            ).count(),
            1,
        )
        self.assertFalse(
            StoreImage.objects.filter(
                store=store,
                image_type=StoreImage.ImageType.ACTUAL_PHOTO,
            ).exists()
        )

    def test_success_explicit_null_image_treated_as_absent(self):
        """spec 에서 image url 은 선택. null 명시 전송도 미전송과 동일하게 처리."""
        body = self._valid_body(
            floorplan_image_url=None,
            actual_photo_url=None,
        )
        response = self._post(body, self.access)
        self.assertEqual(response.status_code, 200)

        store = Store.objects.get(id=response.json()["data"]["store_id"])
        self.assertFalse(StoreImage.objects.filter(store=store).exists())

    def test_unauthorized_without_token(self):
        response = self._post(self._valid_body())
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED_USER")
        # DB 변화 없음
        self.assertEqual(Store.objects.count(), 0)

    def test_unauthorized_with_malformed_token(self):
        response = self._post(self._valid_body(), "not-a-real-token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")
        self.assertEqual(Store.objects.count(), 0)

    def test_missing_required_field_rejected(self):
        """name 누락 — Pydantic ValidationError → INVALID_PARAMETER 422.
        common.handlers.on_validation 가 spec 의 INVALID_PARAMETER 코드로 매핑.
        """
        body = self._valid_body()
        body.pop("name")
        response = self._post(body, self.access)
        self.assertEqual(response.status_code, 422)
        rj = response.json()
        self.assertFalse(rj["success"])
        self.assertEqual(rj["code"], "INVALID_PARAMETER")
        self.assertEqual(Store.objects.count(), 0)

    def test_missing_dimension_rejected(self):
        body = self._valid_body()
        body.pop("depth")
        response = self._post(body, self.access)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")
        self.assertEqual(Store.objects.count(), 0)

    def test_missing_address_rejected(self):
        body = self._valid_body()
        body.pop("address")
        response = self._post(body, self.access)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")
        self.assertEqual(Store.objects.count(), 0)

    def test_two_stores_by_same_user_each_has_manager_membership(self):
        """한 사용자가 매장 2개 생성 — 각 매장에 별도 MANAGER(점장) 멤버십."""
        r1 = self._post(self._valid_body(name="매장1"), self.access)
        r2 = self._post(self._valid_body(name="매장2"), self.access)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

        store1_id = r1.json()["data"]["store_id"]
        store2_id = r2.json()["data"]["store_id"]
        self.assertNotEqual(store1_id, store2_id)

        self.assertEqual(
            StoreMember.objects.filter(
                user=self.user,
                role=StoreMember.Role.MANAGER,
            ).count(),
            2,
        )

    def test_other_users_stores_isolated(self):
        """다른 사용자가 만든 매장에는 멤버십이 안 생김."""
        other = User.objects.create_user(
            email="other@example.com",
            password="X1!abcde",
            name="다른",
            confirmed=True,
        )
        other_access = str(AccessToken.for_user(other))

        r = self._post(self._valid_body(name="other매장"), other_access)
        self.assertEqual(r.status_code, 200)
        other_store_id = r.json()["data"]["store_id"]

        self.assertFalse(
            StoreMember.objects.filter(
                store_id=other_store_id,
                user=self.user,
            ).exists()
        )


_FLOORPLAN_TEMP_MEDIA = tempfile.mkdtemp(prefix="dogo_floorplan_test_")


@override_settings(MEDIA_ROOT=_FLOORPLAN_TEMP_MEDIA)
class StoreFloorplanUploadEndpointTests(TestCase):
    """POST /api/v1/stores/{store_id}/floorplan — multipart 파일 업로드.

    권한: ADMIN_ROLES (PATCH /stores/{id} 와 동일 — STAFF 거부).
    파일 검증: jpg/png + 10MB 이하.
    덮어쓰기: 기존 FLOORPLAN row 가 있으면 image_url 만 교체 (1 row 유지).
    """

    url_tmpl = "/api/v1/stores/{store_id}/floorplan"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_FLOORPLAN_TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(
            email="fp_owner@example.com",
            password="X1!abcde",
            name="오너",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="fp_manager@example.com",
            password="X1!abcde",
            name="매니저",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="fp_staff@example.com",
            password="X1!abcde",
            name="스태프",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="fp_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.owner,
            name="DO-GO 성수",
            address="서울 성동구",
            width=600,
            height=350,
            depth=500,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _png(self, name="plan.png", size=128):
        return SimpleUploadedFile(
            name, b"\x89PNG\r\n\x1a\n" + b"\x00" * size, content_type="image/png"
        )

    def _post(self, store_id, file=None, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        data = {}
        if file is not None:
            data["file"] = file
        return self.client.post(
            self.url_tmpl.format(store_id=store_id), data=data, **headers
        )

    def test_success_owner_uploads_floorplan_creates_image_row(self):
        response = self._post(
            self.store.id, file=self._png(), access_token=self.owner_access
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "도면 이미지가 성공적으로 업로드되었습니다.")

        data = body["data"]
        self.assertEqual(
            set(data.keys()), {"store_id", "floorplan_image_url", "updated_at"}
        )
        self.assertEqual(data["store_id"], self.store.id)
        self.assertTrue(data["floorplan_image_url"].startswith("/media/stores/images/"))
        self.assertTrue(data["floorplan_image_url"].endswith(".png"))

        rows = StoreImage.objects.filter(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
        )
        self.assertEqual(rows.count(), 1)

    def test_success_manager_can_upload(self):
        """ADMIN_ROLES 안의 MANAGER 도 업로드 가능."""
        response = self._post(
            self.store.id, file=self._png(), access_token=self.manager_access
        )
        self.assertEqual(response.status_code, 200)

    def test_success_overwrites_existing_floorplan(self):
        """기존 FLOORPLAN row 가 있으면 image_url 만 교체 — row 1개 유지."""
        StoreImage.objects.create(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
            image_url="stores/images/old.png",
        )

        response = self._post(
            self.store.id, file=self._png("new.png"), access_token=self.owner_access
        )
        self.assertEqual(response.status_code, 200)

        rows = StoreImage.objects.filter(
            store=self.store,
            image_type=StoreImage.ImageType.FLOORPLAN,
        )
        self.assertEqual(rows.count(), 1)
        self.assertNotEqual(rows.first().image_url.name, "stores/images/old.png")

    def test_unsupported_format_returns_415(self):
        bad = SimpleUploadedFile(
            "plan.gif", b"GIF89a" + b"\x00" * 64, content_type="image/gif"
        )
        response = self._post(self.store.id, file=bad, access_token=self.owner_access)

        self.assertEqual(response.status_code, 415)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNSUPPORTED_MEDIA_TYPE")
        self.assertFalse(StoreImage.objects.filter(store=self.store).exists())

    def test_payload_too_large_returns_413(self):
        oversize = SimpleUploadedFile(
            "huge.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1),
            content_type="image/png",
        )
        response = self._post(
            self.store.id, file=oversize, access_token=self.owner_access
        )

        self.assertEqual(response.status_code, 413)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "PAYLOAD_TOO_LARGE")
        self.assertFalse(StoreImage.objects.filter(store=self.store).exists())

    def test_non_member_returns_404_store_not_found(self):
        """비멤버는 STORE_NOT_FOUND (id 존재성 노출 차단 — GET/PATCH 와 동일 정책)."""
        response = self._post(
            self.store.id, file=self._png(), access_token=self.outsider_access
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_staff_returns_403_forbidden(self):
        """STAFF 는 멤버지만 ADMIN_ROLES 밖 — FORBIDDEN_ACCESS."""
        response = self._post(
            self.store.id, file=self._png(), access_token=self.staff_access
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "FORBIDDEN_ACCESS")
        self.assertFalse(StoreImage.objects.filter(store=self.store).exists())

    def test_unauthorized_without_token(self):
        response = self._post(self.store.id, file=self._png())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    def test_nonexistent_store_returns_404(self):
        response = self._post(999999, file=self._png(), access_token=self.owner_access)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_404(self):
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._post(
            self.store.id, file=self._png(), access_token=self.owner_access
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_missing_file_returns_422(self):
        """file 필드 자체 누락 — django-ninja 의 ValidationError → INVALID_PARAMETER 422."""
        response = self._post(self.store.id, file=None, access_token=self.owner_access)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")
