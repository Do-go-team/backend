import json
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from ninja_jwt.tokens import AccessToken

from assets_3d.models import Asset3D
from fixtures.models import FixtureMaster, FixtureVersion
from layouts.models import Layout, LayoutFixture
from stores.models import Store, StoreMember
from users.models import User


def _lf_create(*, layout, fixture_version, **kwargs):
    """테스트용 LayoutFixture 시드 헬퍼. width/height/depth 명시 안 하면 placeholder
    값(10, 10, 10) 자동 채움 — S14P31F106-240 에서 NOT NULL 추가로 인한 필수 채움.

    시나리오상 사이즈가 의미 있는 테스트면 명시적으로 width=... 전달.
    """
    kwargs.setdefault("width", 10)
    kwargs.setdefault("height", 10)
    kwargs.setdefault("depth", 10)
    return LayoutFixture.objects.create(
        layout=layout, fixture_version=fixture_version, **kwargs
    )


class LayoutCreateEndpointTests(TestCase):
    """POST /api/v1/stores/{store_id}/layouts — 레이아웃 시안 생성.

    권한 정책 (CLAUDE.md ## Store Member Roles 참고):
      - ADMIN_ROLES (OWNER/MANAGER/VICE_MANAGER/VMD) 만 생성 가능, STAFF 거부.
      - 비회원/미존재 매장 모두 STORE_NOT_FOUND 404 (ID 프로빙 방지).

    is_active 토글 불변식:
      - 한 매장당 active 레이아웃은 동시에 하나만 존재 가능.
      - 새 레이아웃을 active 로 생성 시 기존 active 자동 비활성화.
      - 소프트딜리트된 레이아웃은 deactivate 대상에서 제외.
    """

    url_tmpl = "/api/v1/stores/{store_id}/layouts"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="Owner123!",
            name="오너",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="vmd@example.com",
            password="Vmd1234!",
            name="VMD",
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
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _post(self, store_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url_tmpl.format(store_id=store_id),
            data=body,
            content_type="application/json",
            **headers,
        )

    # ── 성공 ───────────────────────────────────────────────────────────

    def test_success_owner_creates_layout(self):
        response = self._post(
            self.store.id,
            {
                "name": "2026 S/S 시즌 기획",
                "comment": "입구 쪽 신상품 진열을 강조한 봄 시즌 첫 번째 시안입니다.",
            },
            self.owner_access,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "레이아웃이 성공적으로 생성되었습니다.")

        data = body["data"]
        self.assertEqual(data["store_id"], self.store.id)
        self.assertEqual(data["name"], "2026 S/S 시즌 기획")
        self.assertEqual(
            data["comment"],
            "입구 쪽 신상품 진열을 강조한 봄 시즌 첫 번째 시안입니다.",
        )
        self.assertFalse(data["is_active"])  # 기본값 False
        self.assertIn("layout_id", data)
        self.assertIn("created_at", data)

        # 응답 키 잠금 — 미래 refactor 가 필드 누설하지 못하도록
        self.assertEqual(
            set(data.keys()),
            {"layout_id", "store_id", "name", "comment", "is_active", "created_at"},
        )

        # DB 반영 확인
        self.assertTrue(
            Layout.objects.filter(id=data["layout_id"], store=self.store).exists()
        )

    def test_success_vmd_can_create(self):
        """VMD 도 ADMIN_ROLES 의 일원 — 레이아웃 생성 가능."""
        response = self._post(
            self.store.id,
            {"name": "테스트"},
            self.vmd_access,
        )
        self.assertEqual(response.status_code, 200)

    def test_success_optional_comment_omitted(self):
        response = self._post(
            self.store.id,
            {"name": "코멘트 없는 시안"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["comment"])

    # ── is_active 토글 불변식 ─────────────────────────────────────────

    def test_is_active_true_deactivates_existing_active_layout(self):
        """기존 active 레이아웃은 자동으로 비활성화돼야 한다."""
        existing = Layout.objects.create(
            store=self.store,
            name="기존 활성",
            is_active=True,
        )

        response = self._post(
            self.store.id,
            {"name": "새 활성", "is_active": True},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["is_active"])

        existing.refresh_from_db()
        self.assertFalse(existing.is_active)

        # 매장에 active 가 정확히 1개만 존재
        self.assertEqual(
            Layout.objects.alive().filter(store=self.store, is_active=True).count(),
            1,
        )

    def test_is_active_false_does_not_touch_existing_active(self):
        """is_active=False 생성 시 기존 active 는 그대로 유지."""
        existing = Layout.objects.create(
            store=self.store,
            name="기존 활성",
            is_active=True,
        )

        response = self._post(
            self.store.id,
            {"name": "비활성 시안"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

        existing.refresh_from_db()
        self.assertTrue(existing.is_active)

    def test_is_active_true_does_not_revive_soft_deleted_layouts(self):
        """소프트딜리트된 레이아웃은 deactivate 대상에서 제외 — 부활 방지."""
        dead = Layout.objects.create(
            store=self.store,
            name="삭제된 활성",
            is_active=True,
        )
        dead.deleted_at = timezone.now()
        dead.save(update_fields=["deleted_at"])

        response = self._post(
            self.store.id,
            {"name": "새 활성", "is_active": True},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

        dead.refresh_from_db()
        # is_active 여전히 True (touch 안 함). deleted 상태라 클라이언트엔 안 보임.
        self.assertTrue(dead.is_active)
        self.assertIsNotNone(dead.deleted_at)

    def test_active_layout_in_other_store_is_not_affected(self):
        """다른 매장의 active 레이아웃은 영향받지 않아야 한다."""
        other_store = Store.objects.create(
            user=self.owner,
            name="다른 매장",
            address="부산",
            width=500,
            height=300,
            depth=400,
        )
        StoreMember.objects.create(
            store=other_store,
            user=self.owner,
            role=StoreMember.Role.OWNER,
        )
        other_active = Layout.objects.create(
            store=other_store,
            name="다른 매장 활성",
            is_active=True,
        )

        response = self._post(
            self.store.id,
            {"name": "이 매장 새 활성", "is_active": True},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

        other_active.refresh_from_db()
        self.assertTrue(other_active.is_active)

    # ── 권한 ──────────────────────────────────────────────────────────

    def test_staff_forbidden(self):
        response = self._post(
            self.store.id,
            {"name": "스태프 시안"},
            self.staff_access,
        )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN_ACCESS")

    def test_outsider_returns_store_not_found(self):
        """비회원은 매장 존재 여부도 노출 안 함 — STORE_NOT_FOUND 404."""
        response = self._post(
            self.store.id,
            {"name": "외부 시안"},
            self.outsider_access,
        )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "STORE_NOT_FOUND")

    def test_unauthenticated_returns_401(self):
        response = self._post(self.store.id, {"name": "비인증"})
        self.assertEqual(response.status_code, 401)

    # ── 매장 미존재 ───────────────────────────────────────────────────

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._post(99999, {"name": "허공"}, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        """삭제된 매장은 OWNER 에게도 보이지 않음."""
        self.store.soft_delete()
        response = self._post(
            self.store.id,
            {"name": "삭제된 매장"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    # ── 입력 검증 ─────────────────────────────────────────────────────

    def test_missing_name_returns_validation_error(self):
        """name 필수 — pydantic validation 실패 시 422."""
        response = self._post(self.store.id, {}, self.owner_access)
        self.assertEqual(response.status_code, 422)


class LayoutListEndpointTests(TestCase):
    """GET /api/v1/stores/{store_id}/layouts — 매장의 레이아웃 시안 목록.

    권한: any membership (조회는 STAFF 포함 모두 허용).

    정렬 규칙:
      1. is_active=True 인 레이아웃이 최상단 (한 매장당 최대 1개)
      2. 그 다음은 created_at desc (최신 시안 위로)

    소프트딜리트 매장/레이아웃은 응답에서 모두 제외.
    """

    url_tmpl = "/api/v1/stores/{store_id}/layouts"

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
            width=1000,
            height=300,
            depth=800,
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

    # ── 정렬 ──────────────────────────────────────────────────────────

    def test_sort_active_first_then_created_desc(self):
        """active 가 항상 최상단, 나머지는 created_at desc."""
        # 의도적으로 created 순서와 active 위치를 어긋나게 배치
        oldest = Layout.objects.create(store=self.store, name="가장 오래된")
        middle = Layout.objects.create(store=self.store, name="중간")
        newest_inactive = Layout.objects.create(store=self.store, name="최신 비활성")
        # active 는 시간상 가장 오래되지 않은 위치에 배치 (정렬 우선순위 검증용)
        active = Layout.objects.create(
            store=self.store, name="현재 적용중", is_active=True
        )
        # active 보다 더 나중에 생성된 비활성 (active 가 최상단인지 확실히 검증)
        post_active_inactive = Layout.objects.create(
            store=self.store, name="active 이후 생성"
        )

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "레이아웃 목록 조회에 성공했습니다.")

        ids = [item["layout_id"] for item in body["data"]["layouts"]]
        # active 가 created_at 무시하고 최상단
        self.assertEqual(ids[0], active.id)
        # 나머지는 created_at desc
        self.assertEqual(
            ids[1:],
            [post_active_inactive.id, newest_inactive.id, middle.id, oldest.id],
        )

    def test_sort_no_active_orders_by_created_desc(self):
        """active 가 하나도 없으면 모두 created_at desc."""
        a = Layout.objects.create(store=self.store, name="A")
        b = Layout.objects.create(store=self.store, name="B")
        c = Layout.objects.create(store=self.store, name="C")

        response = self._get(self.store.id, self.owner_access)
        ids = [item["layout_id"] for item in response.json()["data"]["layouts"]]
        self.assertEqual(ids, [c.id, b.id, a.id])

    # ── 응답 형태 ─────────────────────────────────────────────────────

    def test_response_payload_keys(self):
        """응답 row 의 키 잠금 — 미래 refactor 가 필드 누설하지 못하도록."""
        Layout.objects.create(store=self.store, name="단일", comment="C")

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(set(data.keys()), {"layouts"})
        item = data["layouts"][0]
        self.assertEqual(
            set(item.keys()),
            {"layout_id", "name", "comment", "is_active", "created_at", "updated_at"},
        )

    def test_empty_store_returns_empty_list(self):
        """레이아웃이 하나도 없는 매장은 빈 배열 + 성공."""
        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["layouts"], [])

    # ── 격리 ──────────────────────────────────────────────────────────

    def test_other_store_layouts_are_isolated(self):
        """다른 매장의 레이아웃은 결과에 포함되지 않아야 한다."""
        other_store = Store.objects.create(
            user=self.owner,
            name="다른 매장",
            address="부산",
            width=500,
            height=300,
            depth=400,
        )
        StoreMember.objects.create(
            store=other_store,
            user=self.owner,
            role=StoreMember.Role.OWNER,
        )
        Layout.objects.create(store=other_store, name="다른 매장 레이아웃")
        mine = Layout.objects.create(store=self.store, name="내 매장 레이아웃")

        response = self._get(self.store.id, self.owner_access)
        ids = [item["layout_id"] for item in response.json()["data"]["layouts"]]
        self.assertEqual(ids, [mine.id])

    def test_soft_deleted_layouts_excluded(self):
        """소프트딜리트된 레이아웃은 목록에서 제외."""
        alive = Layout.objects.create(store=self.store, name="살아있음")
        dead = Layout.objects.create(store=self.store, name="삭제됨")
        dead.deleted_at = timezone.now()
        dead.save(update_fields=["deleted_at"])

        response = self._get(self.store.id, self.owner_access)
        ids = [item["layout_id"] for item in response.json()["data"]["layouts"]]
        self.assertEqual(ids, [alive.id])

    # ── 권한 ──────────────────────────────────────────────────────────

    def test_staff_can_list(self):
        """조회는 read-only 라 STAFF 도 가능."""
        Layout.objects.create(store=self.store, name="staff 도 보임")

        response = self._get(self.store.id, self.staff_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]["layouts"]), 1)

    def test_outsider_returns_store_not_found(self):
        Layout.objects.create(store=self.store, name="외부엔 안 보임")

        response = self._get(self.store.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "STORE_NOT_FOUND")

    def test_unauthenticated_returns_401(self):
        response = self._get(self.store.id)
        self.assertEqual(response.status_code, 401)

    # ── 매장 미존재 ───────────────────────────────────────────────────

    def test_nonexistent_store_returns_store_not_found(self):
        response = self._get(99999, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")

    def test_soft_deleted_store_returns_store_not_found(self):
        """삭제된 매장은 OWNER 에게도 보이지 않음."""
        Layout.objects.create(store=self.store, name="매장이 곧 삭제됨")
        self.store.soft_delete()

        response = self._get(self.store.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")


class LayoutDetailEndpointTests(TestCase):
    """GET /api/v1/layouts/{layout_id} — 3D 캔버스 즉시 렌더용 Aggregate.

    한 번의 응답으로 (1) 레이아웃 메타 (2) 매장 물리 규격 (3) 배치된 집기들의
    좌표 + 마스터 정보 + 3D 모델 URL 까지 묶어서 내려준다.

    권한: any membership (조회는 STAFF 포함). 비회원/미존재 레이아웃/소프트딜리트
    레이아웃·매장 모두 LAYOUT_NOT_FOUND 404 로 묶음 (ID 프로빙 방지).

    스냅샷 정책: 본사 차원에서 fixture_master / fixture_version 이 소프트딜리트돼도
    레이아웃 detail 에는 그대로 노출된다 (작성 시점 스냅샷 보존). products 156 의
    DISCONTINUED 표면화 정책과 같은 결.
    """

    url_tmpl = "/api/v1/layouts/{layout_id}"

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
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.layout = Layout.objects.create(
            store=self.store,
            name="2026 S/S 시즌 기획",
            comment="입구 쪽 신상품 진열 강조",
            is_active=False,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _get(self, layout_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url_tmpl.format(layout_id=layout_id), **headers)

    def _make_fixture_version(self, name="중앙 매대 A형", w=120, h=90, d=60):
        master = FixtureMaster.objects.create(
            user=self.owner,
            name=name,
            width=w,
            height=h,
            depth=d,
        )
        version = FixtureVersion.objects.create(
            fixture_master=master,
            version_name="v1",
        )
        return master, version

    # ── 성공: 메타 + 빈 fixtures ─────────────────────────────────────

    def test_success_no_fixtures_returns_full_envelope(self):
        response = self._get(self.layout.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "레이아웃 상세 정보 조회에 성공했습니다.")

        data = body["data"]
        self.assertEqual(data["layout_id"], self.layout.id)
        self.assertEqual(data["store_id"], self.store.id)
        self.assertEqual(data["name"], "2026 S/S 시즌 기획")
        self.assertEqual(data["comment"], "입구 쪽 신상품 진열 강조")
        self.assertFalse(data["is_active"])
        self.assertEqual(
            data["store_dimensions"],
            {"width": 1000, "height": 300, "depth": 800},
        )
        self.assertEqual(data["fixtures"], [])

        # 응답 키 잠금
        self.assertEqual(
            set(data.keys()),
            {
                "layout_id",
                "store_id",
                "name",
                "comment",
                "is_active",
                "floorplan_image_url",
                "store_dimensions",
                "fixtures",
            },
        )
        # 기존 layout 은 도면 미설정 → null
        self.assertIsNone(data["floorplan_image_url"])

    # ── 성공: Aggregate (fixtures + asset_3d) ─────────────────────────

    def test_success_aggregates_fixtures_and_assets(self):
        master_a, version_a = self._make_fixture_version(
            name="중앙 매대 A형", w=120, h=90, d=60
        )
        master_b, version_b = self._make_fixture_version(
            name="벽면 진열대", w=200, h=180, d=40
        )

        Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=master_a.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/center_fixture_A.glb",
        )
        # master_b 는 일부러 Asset3D 없음 → fixture_info.asset_3d == None 확인용

        fx_a = _lf_create(
            layout=self.layout,
            fixture_version=version_a,
            world_pos_x=1200,
            world_pos_y=0,
            world_pos_z=3500,
            world_rot_y=90,
        )
        fx_b = _lf_create(
            layout=self.layout,
            fixture_version=version_b,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )

        response = self._get(self.layout.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        fixtures = response.json()["data"]["fixtures"]

        # 정렬은 LayoutFixture.id asc
        self.assertEqual([f["layout_fixture_id"] for f in fixtures], [fx_a.id, fx_b.id])

        a, b = fixtures
        self.assertEqual(a["fixture_id"], master_a.id)
        self.assertEqual(a["fixture_version_id"], version_a.id)
        self.assertEqual(a["world_pos_x"], 1200)
        self.assertEqual(a["world_rot_y"], 90)
        self.assertEqual(
            a["fixture_info"],
            {
                "name": "중앙 매대 A형",
                "width": 120,
                "height": 90,
                "depth": 60,
                "asset_3d": {
                    "file_format": "GLB",
                    "model_url": a["fixture_info"]["asset_3d"]["model_url"],
                },
            },
        )
        # MEDIA_URL 접두사가 붙은 절대 경로로 직렬화되는지 확인
        self.assertTrue(
            a["fixture_info"]["asset_3d"]["model_url"].endswith("center_fixture_A.glb")
        )

        self.assertEqual(b["fixture_info"]["name"], "벽면 진열대")
        self.assertEqual(b["fixture_id"], master_b.id)
        self.assertIsNone(b["fixture_info"]["asset_3d"])

        # fixture_info 키 잠금
        self.assertEqual(
            set(a["fixture_info"].keys()),
            {"name", "width", "height", "depth", "asset_3d"},
        )

    def test_multiple_assets_picks_latest_per_master(self):
        """같은 fixture_master 에 Asset3D 여러 건 → created_at 최신만 채택."""
        master, version = self._make_fixture_version()

        Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=master.id,
            file_format=Asset3D.FileFormat.OBJ,
            model_url="assets/3d/old.obj",
        )
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=master.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/new.glb",
        )

        _lf_create(
            layout=self.layout,
            fixture_version=version,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )

        response = self._get(self.layout.id, self.owner_access)
        asset = response.json()["data"]["fixtures"][0]["fixture_info"]["asset_3d"]
        self.assertEqual(asset["file_format"], "GLB")
        self.assertTrue(asset["model_url"].endswith("new.glb"))
        self.assertNotIn("old.obj", asset["model_url"])

    def test_asset_for_other_target_type_is_ignored(self):
        """target_type=PRODUCT 으로 같은 ID 에 Asset3D 가 있어도 FIXTURE 와 격리."""
        master, version = self._make_fixture_version()

        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/wrong_product.glb",
        )

        _lf_create(
            layout=self.layout,
            fixture_version=version,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )

        response = self._get(self.layout.id, self.owner_access)
        self.assertIsNone(
            response.json()["data"]["fixtures"][0]["fixture_info"]["asset_3d"]
        )

    # ── 스냅샷 정책: 소프트딜리트된 fixture 도 노출 ────────────────────

    def test_soft_deleted_fixture_master_still_visible(self):
        """본사 차원의 단종은 detail 표시를 지우지 않는다 (스냅샷)."""
        master, version = self._make_fixture_version()
        _lf_create(
            layout=self.layout,
            fixture_version=version,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )
        master.soft_delete()
        version.soft_delete()

        response = self._get(self.layout.id, self.owner_access)
        self.assertEqual(response.status_code, 200)
        fixtures = response.json()["data"]["fixtures"]
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["fixture_info"]["name"], master.name)

    # ── 권한 ──────────────────────────────────────────────────────────

    def test_staff_can_view(self):
        response = self._get(self.layout.id, self.staff_access)
        self.assertEqual(response.status_code, 200)

    def test_outsider_returns_layout_not_found(self):
        """비회원은 레이아웃 존재 여부 비노출 — LAYOUT_NOT_FOUND 404."""
        response = self._get(self.layout.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "LAYOUT_NOT_FOUND")
        self.assertEqual(
            body["message"],
            "존재하지 않거나 접근 권한이 없는 레이아웃입니다.",
        )

    def test_unauthenticated_returns_401(self):
        response = self._get(self.layout.id)
        self.assertEqual(response.status_code, 401)

    # ── 미존재 / 소프트딜리트 ─────────────────────────────────────────

    def test_nonexistent_layout_returns_layout_not_found(self):
        response = self._get(99999, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_soft_deleted_layout_returns_layout_not_found(self):
        """삭제된 레이아웃은 OWNER 에게도 보이지 않음."""
        self.layout.soft_delete()
        response = self._get(self.layout.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_soft_deleted_store_returns_layout_not_found(self):
        """매장이 삭제되면 그 안의 레이아웃도 함께 비노출."""
        self.store.soft_delete()
        response = self._get(self.layout.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")


class LayoutDeleteEndpointTests(TestCase):
    """DELETE /api/v1/layouts/{layout_id} — 레이아웃 시안 소프트 삭제.

    권한: ADMIN_ROLES (스펙상 코드 'FORBIDDEN' — 다른 endpoint 의
    'FORBIDDEN_ACCESS'/'FORBIDDEN_ACTION' 와 다름).

    안전장치: is_active=True 레이아웃은 ACTIVE_LAYOUT_DELETE_DENIED (409) 로 차단.
    소프트 삭제이므로 row 는 보존, deleted_at 만 세팅.
    """

    url_tmpl = "/api/v1/layouts/{layout_id}"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="Owner123!",
            name="오너",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="vmd@example.com",
            password="Vmd1234!",
            name="VMD",
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
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.inactive = Layout.objects.create(
            store=self.store,
            name="비활성 시안",
            is_active=False,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _delete(self, layout_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.delete(self.url_tmpl.format(layout_id=layout_id), **headers)

    # ── 성공 ──────────────────────────────────────────────────────────

    def test_success_owner_soft_deletes_inactive_layout(self):
        response = self._delete(self.inactive.id, self.owner_access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "레이아웃이 성공적으로 삭제되었습니다.")
        self.assertEqual(body["data"], {"deleted_layout_id": self.inactive.id})

        # 소프트 삭제 — row 보존 + deleted_at 세팅
        self.inactive.refresh_from_db()
        self.assertIsNotNone(self.inactive.deleted_at)
        # alive() 에서는 제외
        self.assertFalse(Layout.objects.alive().filter(id=self.inactive.id).exists())

    def test_success_vmd_can_delete(self):
        """VMD 도 ADMIN_ROLES 의 일원."""
        response = self._delete(self.inactive.id, self.vmd_access)
        self.assertEqual(response.status_code, 200)

    # ── 안전장치 ──────────────────────────────────────────────────────

    def test_active_layout_blocked(self):
        """is_active=True 레이아웃은 ACTIVE_LAYOUT_DELETE_DENIED 로 차단."""
        active = Layout.objects.create(
            store=self.store,
            name="현재 적용중",
            is_active=True,
        )

        response = self._delete(active.id, self.owner_access)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "ACTIVE_LAYOUT_DELETE_DENIED")
        self.assertIn("비활성화", body["message"])

        # row 변경 없어야
        active.refresh_from_db()
        self.assertIsNone(active.deleted_at)
        self.assertTrue(active.is_active)

    def test_active_layout_can_be_deleted_after_deactivation(self):
        """비활성화 후엔 삭제 가능 — 안전장치 우회 절차 확인."""
        active = Layout.objects.create(
            store=self.store,
            name="비활성화 후 삭제할 시안",
            is_active=True,
        )

        blocked = self._delete(active.id, self.owner_access)
        self.assertEqual(blocked.status_code, 409)

        active.is_active = False
        active.save(update_fields=["is_active"])

        ok_resp = self._delete(active.id, self.owner_access)
        self.assertEqual(ok_resp.status_code, 200)

    # ── 권한 ──────────────────────────────────────────────────────────

    def test_staff_forbidden(self):
        """STAFF 는 FORBIDDEN (스펙상 'FORBIDDEN_ACCESS' 가 아니라 'FORBIDDEN')."""
        response = self._delete(self.inactive.id, self.staff_access)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN")
        self.assertEqual(body["message"], "해당 레이아웃을 삭제할 권한이 없습니다.")

        # row 변경 없어야
        self.inactive.refresh_from_db()
        self.assertIsNone(self.inactive.deleted_at)

    def test_outsider_returns_layout_not_found(self):
        """비회원은 LAYOUT_NOT_FOUND — FORBIDDEN 보다 우선 (존재성 비노출)."""
        response = self._delete(self.inactive.id, self.outsider_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_unauthenticated_returns_401(self):
        response = self._delete(self.inactive.id)
        self.assertEqual(response.status_code, 401)

    # ── 미존재 / 소프트딜리트 / 멱등 ──────────────────────────────────

    def test_nonexistent_layout_returns_layout_not_found(self):
        response = self._delete(99999, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_already_deleted_layout_returns_layout_not_found(self):
        """이미 삭제된 레이아웃을 다시 삭제 — alive() 필터로 LAYOUT_NOT_FOUND."""
        self.inactive.soft_delete()

        response = self._delete(self.inactive.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_soft_deleted_store_returns_layout_not_found(self):
        """매장이 삭제되면 OWNER 도 그 안의 레이아웃을 삭제할 수 없음."""
        self.store.soft_delete()

        response = self._delete(self.inactive.id, self.owner_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")


class LayoutUpdateEndpointTests(TestCase):
    """PATCH /api/v1/layouts/{layout_id} — 레이아웃 메타 + fixtures 동시 수정.

    스코프상 endpoint 고유 비즈니스 로직만 검증 (helper 단의 not_found/권한
    분기는 stores helper / 다른 layouts endpoint 가 이미 cover):
      - PATCH 메타 partial update (name/comment/is_active)
      - fixtures bulk-sync 3-rule (UPDATE/INSERT/DELETE)
      - fixtures: [] = 전체 삭제 (4-A 결정)
      - is_active=true 토글로 다른 active 자동 비활성화
      - INVALID_FIXTURE_DATA 422 — 옵션 b strict (모든 필드 required + 범위/존재성)
      - 검증 실패 시 어떤 변경도 일어나지 않음 (transaction atomicity)
      - 권한 + helper trigger 1개씩
    """

    url_tmpl = "/api/v1/layouts/{layout_id}"

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
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.layout = Layout.objects.create(
            store=self.store,
            name="2026 S/S 시즌 기획",
            comment="기존 코멘트",
            is_active=False,
        )

        # 시드 fixtures 2개 (UPDATE/DELETE 케이스 검증용)
        self.master = FixtureMaster.objects.create(
            user=self.owner,
            name="중앙 매대",
            width=120,
            height=90,
            depth=60,
        )
        self.version = FixtureVersion.objects.create(
            fixture_master=self.master,
            version_name="v1",
        )
        self.master_b = FixtureMaster.objects.create(
            user=self.owner,
            name="벽면 진열대",
            width=200,
            height=180,
            depth=40,
        )
        self.version_b = FixtureVersion.objects.create(
            fixture_master=self.master_b,
            version_name="v1",
        )
        self.fx_keep = _lf_create(
            layout=self.layout,
            fixture_version=self.version,
            world_pos_x=100,
            world_pos_y=0,
            world_pos_z=200,
            world_rot_y=0,
        )
        self.fx_drop = _lf_create(
            layout=self.layout,
            fixture_version=self.version,
            world_pos_x=500,
            world_pos_y=0,
            world_pos_z=600,
            world_rot_y=90,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _patch(self, layout_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            self.url_tmpl.format(layout_id=layout_id),
            data=body,
            content_type="application/json",
            **headers,
        )

    # ── 메타-only 모드 (fixtures 키 부재) ─────────────────────────────

    def test_meta_only_update_omits_fixture_counts(self):
        """fixtures 키가 요청에 없으면 응답에서도 fixtures_updated_count /
        fixtures_deleted_count 미포함 (메타-only 모드)."""
        before_updated_at = self.layout.updated_at

        response = self._patch(
            self.layout.id,
            {"name": "변경된 이름", "is_active": True, "comment": "새 코멘트"},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "레이아웃 정보가 성공적으로 수정되었습니다.")

        data = body["data"]
        self.assertEqual(data["layout_id"], self.layout.id)
        self.assertEqual(data["name"], "변경된 이름")
        self.assertTrue(data["is_active"])
        self.assertNotIn("fixtures_updated_count", data)
        self.assertNotIn("fixtures_deleted_count", data)

        # 응답 키 잠금 (메타-only 모드)
        self.assertEqual(
            set(data.keys()), {"layout_id", "name", "is_active", "updated_at"}
        )

        # DB 반영 확인 + updated_at 갱신
        self.layout.refresh_from_db()
        self.assertEqual(self.layout.name, "변경된 이름")
        self.assertEqual(self.layout.comment, "새 코멘트")
        self.assertTrue(self.layout.is_active)
        self.assertGreater(self.layout.updated_at, before_updated_at)

        # fixtures 는 그대로 (2개)
        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout).count(), 2)

    # ── Bulk-sync 3-rule 핵심 ─────────────────────────────────────────

    def test_bulk_sync_insert_update_delete_in_one_request(self):
        """3-rule: keep 의 좌표 UPDATE + 신규 INSERT + drop 누락 → DELETE."""
        before_updated_at = self.layout.updated_at

        response = self._patch(
            self.layout.id,
            {
                "fixtures": [
                    # UPDATE — fx_keep 의 좌표 변경
                    {
                        "layout_fixture_id": self.fx_keep.id,
                        "fixture_version_id": self.version.id,
                        "world_pos_x": 999,
                        "world_pos_y": 0,
                        "world_pos_z": 999,
                        "world_rot_y": 180,
                    },
                    # INSERT — 신규 (layout_fixture_id 없음)
                    {
                        "fixture_version_id": self.version_b.id,
                        "world_pos_x": 0,
                        "world_pos_y": 0,
                        "world_pos_z": 0,
                        "world_rot_y": 0,
                    },
                    # fx_drop 은 요청에서 누락 → DELETE
                ],
            },
            self.owner_access,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["fixtures_updated_count"], 2)  # UPDATE + INSERT
        self.assertEqual(data["fixtures_deleted_count"], 1)  # fx_drop

        # DB 검증
        remaining = LayoutFixture.objects.filter(layout=self.layout).order_by("id")
        self.assertEqual(remaining.count(), 2)
        self.fx_keep.refresh_from_db()
        self.assertEqual(self.fx_keep.world_pos_x, 999)
        self.assertEqual(self.fx_keep.world_rot_y, 180)
        self.assertFalse(LayoutFixture.objects.filter(id=self.fx_drop.id).exists())

        # 메타 변경 없어도 updated_at bump
        self.layout.refresh_from_db()
        self.assertGreater(self.layout.updated_at, before_updated_at)

    def test_empty_fixtures_array_deletes_all(self):
        """fixtures: [] = 전체 삭제 (4-A 결정)."""
        response = self._patch(
            self.layout.id,
            {"fixtures": []},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["fixtures_updated_count"], 0)
        self.assertEqual(data["fixtures_deleted_count"], 2)

        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout).count(), 0)

    # ── is_active 토글 (생성 endpoint 와 동일 정책) ───────────────────

    def test_is_active_true_deactivates_other_active_in_same_store(self):
        other_active = Layout.objects.create(
            store=self.store,
            name="기존 활성",
            is_active=True,
        )

        response = self._patch(
            self.layout.id,
            {"is_active": True},
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["is_active"])

        other_active.refresh_from_db()
        self.assertFalse(other_active.is_active)

        self.assertEqual(
            Layout.objects.alive().filter(store=self.store, is_active=True).count(),
            1,
        )

    # ── INVALID_FIXTURE_DATA (4 트리거 — 각각 다른 코드 path) ────────

    def test_world_rot_y_normalized_modulo_360(self):
        """3D 캔버스에서 누적 회전값(예: 720도, -90도) 보내도 0~359 로 wrap.
        한 바퀴 = 360도라 361 ≡ 1, -90 ≡ 270 (의미적으로 동일)."""
        # INSERT 케이스 — 361 → 1
        # UPDATE 케이스 — -90 → 270
        response = self._patch(
            self.layout.id,
            {
                "fixtures": [
                    {
                        "layout_fixture_id": self.fx_keep.id,
                        "fixture_version_id": self.version.id,
                        "world_pos_x": 0,
                        "world_pos_y": 0,
                        "world_pos_z": 0,
                        "world_rot_y": -90,
                    },
                    {
                        "fixture_version_id": self.version.id,
                        "world_pos_x": 0,
                        "world_pos_y": 0,
                        "world_pos_z": 0,
                        "world_rot_y": 361,
                    },
                ]
            },
            self.owner_access,
        )
        self.assertEqual(response.status_code, 200)

        self.fx_keep.refresh_from_db()
        self.assertEqual(self.fx_keep.world_rot_y, 270)  # -90 % 360
        # 신규 INSERT row 의 rot_y == 1
        new_row = LayoutFixture.objects.exclude(
            id__in=[self.fx_keep.id, self.fx_drop.id]
        ).get(layout=self.layout)
        self.assertEqual(new_row.world_rot_y, 1)

    def test_invalid_fixture_data_missing_required_field_on_insert(self):
        """옵션 b: INSERT 시 모든 필드 required."""
        response = self._patch(
            self.layout.id,
            {
                "fixtures": [
                    {
                        "fixture_version_id": self.version.id,
                        "world_pos_x": 0,
                        "world_pos_y": 0,
                        # world_pos_z / world_rot_y 누락
                    }
                ]
            },
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_FIXTURE_DATA")

    def test_invalid_fixture_data_unknown_fixture_version(self):
        response = self._patch(
            self.layout.id,
            {
                "fixtures": [
                    {
                        "fixture_version_id": 999999,
                        "world_pos_x": 0,
                        "world_pos_y": 0,
                        "world_pos_z": 0,
                        "world_rot_y": 0,
                    }
                ]
            },
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_FIXTURE_DATA")

    def test_invalid_fixture_data_cross_layout_fixture_id(self):
        """다른 레이아웃의 layout_fixture_id 참조는 INVALID_FIXTURE_DATA — 보안."""
        other_layout = Layout.objects.create(store=self.store, name="다른 레이아웃")
        foreign_lf = _lf_create(
            layout=other_layout,
            fixture_version=self.version,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )

        response = self._patch(
            self.layout.id,
            {
                "fixtures": [
                    {
                        "layout_fixture_id": foreign_lf.id,
                        "fixture_version_id": self.version.id,
                        "world_pos_x": 1,
                        "world_pos_y": 1,
                        "world_pos_z": 1,
                        "world_rot_y": 0,
                    }
                ]
            },
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_FIXTURE_DATA")

        # foreign_lf 가 변경되지 않음 (cross-layout 격리 검증)
        foreign_lf.refresh_from_db()
        self.assertEqual(foreign_lf.world_pos_x, 0)

    # ── 트랜잭션 atomicity ────────────────────────────────────────────

    def test_atomicity_no_partial_change_on_validation_failure(self):
        """검증 실패 시 메타도 fixtures 도 변경되지 않아야 한다 — fail-fast +
        transaction.atomic 이중 보장."""
        before = {
            "name": self.layout.name,
            "is_active": self.layout.is_active,
            "fx_keep_pos_x": self.fx_keep.world_pos_x,
            "fx_count": LayoutFixture.objects.filter(layout=self.layout).count(),
        }

        response = self._patch(
            self.layout.id,
            {
                "name": "변경 시도",
                "is_active": True,
                "fixtures": [
                    # 첫 row 는 valid
                    {
                        "layout_fixture_id": self.fx_keep.id,
                        "fixture_version_id": self.version.id,
                        "world_pos_x": 9999,
                        "world_pos_y": 0,
                        "world_pos_z": 0,
                        "world_rot_y": 0,
                    },
                    # 두 번째 row 가 INVALID — 전체 롤백되어야 함
                    # (미존재 fixture_version_id 로 트리거 — rot_y 는 정규화돼서 더 이상 트리거 못 함)
                    {
                        "fixture_version_id": 999999,
                        "world_pos_x": 0,
                        "world_pos_y": 0,
                        "world_pos_z": 0,
                        "world_rot_y": 0,
                    },
                ],
            },
            self.owner_access,
        )
        self.assertEqual(response.status_code, 422)

        self.layout.refresh_from_db()
        self.fx_keep.refresh_from_db()
        self.assertEqual(self.layout.name, before["name"])
        self.assertEqual(self.layout.is_active, before["is_active"])
        self.assertEqual(self.fx_keep.world_pos_x, before["fx_keep_pos_x"])
        self.assertEqual(
            LayoutFixture.objects.filter(layout=self.layout).count(),
            before["fx_count"],
        )

    # ── 권한 + helper trigger (lean: 각 1개) ─────────────────────────

    def test_staff_can_update(self):
        """STAFF 도 layout 내부 집기 수정 가능 (S14P31F106-240 권한 완화).

        이전: STAFF 거부 (FORBIDDEN_ACCESS 403). 정책 변경 — 진열 조작은 일상 작업이라
        매장 멤버 누구나 (STAFF 포함). create/delete 는 admin-tier 그대로.
        """
        response = self._patch(
            self.layout.id,
            {"name": "스태프 수정 시도"},
            self.staff_access,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_outsider_returns_layout_not_found(self):
        response = self._patch(
            self.layout.id,
            {"name": "외부 시도"},
            self.outsider_access,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")


class LayoutExportEndpointTests(TestCase):
    """POST /api/v1/stores/{store_id}/layouts/{layout_id}/export — 평면도 PDF.

    spec: 매장 멤버라면 누구나 export 가능 (read 권한과 같은 결). 응답은
    file_id/file_name/download_url/expires_at — 다운로드 URL 은 MEDIA 단계에선
    Django MEDIA_URL 기반, S3 도입 시 helper 만 swap 하면 응답 schema 무변경.
    """

    url_tmpl = "/api/v1/stores/{store_id}/layouts/{layout_id}/export"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner.export@example.com",
            password="Owner123!",
            name="export오너",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="outsider.export@example.com",
            password="Out123!",
            name="외부",
            confirmed=True,
        )

        self.store = Store.objects.create(
            user=self.owner,
            name="DO-GO 평면도 매장",
            address="서울 성동구",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )

        self.layout = Layout.objects.create(
            store=self.store,
            name="평면도 테스트 시안",
            comment=None,
            is_active=False,
        )

        # 한 개의 fixture 배치 (그리기 코드 path 실제 통과)
        master = FixtureMaster.objects.create(
            user=self.owner, name="중앙 매대", width=120, height=90, depth=60
        )
        version = FixtureVersion.objects.create(
            fixture_master=master, version_name="v1"
        )
        _lf_create(
            layout=self.layout,
            fixture_version=version,
            world_pos_x=500,
            world_pos_y=0,
            world_pos_z=400,
            world_rot_y=0,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _post(self, store_id, layout_id, access_token=None, body=None):
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url_tmpl.format(store_id=store_id, layout_id=layout_id),
            data=body or {},
            content_type="application/json",
        )

    def test_non_member_returns_layout_not_found(self):
        """비회원 호출 — ID 프로빙 차단을 위해 LAYOUT_NOT_FOUND 404."""
        response = self._post(
            self.store.id, self.layout.id, self.outsider_access, body={}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_layout_store_mismatch_when_store_id_differs(self):
        """spec path 의 store_id 가 layout.store_id 와 다르면 LAYOUT_STORE_MISMATCH."""
        other_store = Store.objects.create(
            user=self.owner,
            name="다른 매장",
            address="서울",
            max_admin_count=5,
            width=500,
            height=200,
            depth=500,
        )
        StoreMember.objects.create(
            store=other_store, user=self.owner, role=StoreMember.Role.OWNER
        )
        response = self._post(
            other_store.id, self.layout.id, self.owner_access, body={}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "LAYOUT_STORE_MISMATCH")

    def test_success_returns_pdf_metadata(self):
        """정상 호출 — 응답 schema (file_id, file_name, download_url, expires_at)."""
        response = self._post(self.store.id, self.layout.id, self.owner_access, body={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertIn("file_id", data)
        self.assertIn("file_name", data)
        self.assertIn("download_url", data)
        self.assertIn("expires_at", data)
        self.assertTrue(data["file_id"].startswith("pdf_"))
        self.assertTrue(data["file_name"].endswith(".pdf"))
        self.assertIn("/exports/", data["download_url"])

    def test_options_accepted_and_applied(self):
        """모든 옵션 (paper_size, orientation, include_labels, show_grid) 정상 처리."""
        body = {
            "paper_size": "A3",
            "orientation": "portrait",
            "include_labels": False,
            "show_grid": True,
        }
        response = self._post(
            self.store.id, self.layout.id, self.owner_access, body=body
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])


class LayoutFixtureSizeTests(TestCase):
    """PATCH /api/v1/layouts/{layout_id} 의 사이즈 처리 (S14P31F106-240).

    - INSERT 시 사이즈 누락 → fixture_master 값 자동 복사
    - INSERT 시 사이즈 명시 → 그 값 저장
    - UPDATE 시 사이즈 변경 → 해당 row 만, 다른 row/master 영향 없음
    - 사이즈 0 또는 음수 → INVALID_FIXTURE_DATA 422
    """

    url_tmpl = "/api/v1/layouts/{layout_id}"

    def setUp(self):
        self.user = User.objects.create_user(
            email="size_owner@example.com",
            password="Pwd1234!",
            name="사이즈오너",
            confirmed=True,
        )
        self.store = Store.objects.create(
            user=self.user,
            name="사이즈매장",
            address="서울",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store, user=self.user, role=StoreMember.Role.MANAGER
        )
        self.layout = Layout.objects.create(
            store=self.store, name="사이즈레이아웃", is_active=False
        )
        self.master = FixtureMaster.objects.create(
            user=self.user, name="원형 매대", width=100, height=200, depth=300
        )
        self.version = FixtureVersion.objects.create(
            fixture_master=self.master, version_name="v1"
        )
        self.access = str(AccessToken.for_user(self.user))

    def _patch(self, body):
        self.client.cookies["access_token"] = self.access
        return self.client.patch(
            self.url_tmpl.format(layout_id=self.layout.id),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_insert_size_defaults_to_master(self):
        """INSERT row 의 width/height/depth 누락 → master 값 자동 복사."""
        body = {
            "fixtures": [
                {
                    "fixture_version_id": self.version.id,
                    "world_pos_x": 0,
                    "world_pos_y": 0,
                    "world_pos_z": 0,
                    "world_rot_y": 0,
                }
            ]
        }
        resp = self._patch(body)
        self.assertEqual(resp.status_code, 200)
        lf = LayoutFixture.objects.get(layout=self.layout)
        self.assertEqual(lf.width, 100)
        self.assertEqual(lf.height, 200)
        self.assertEqual(lf.depth, 300)

    def test_insert_size_explicit(self):
        """INSERT row 의 width 명시 → 그 값 저장, 누락 필드만 master 값."""
        body = {
            "fixtures": [
                {
                    "fixture_version_id": self.version.id,
                    "world_pos_x": 0,
                    "world_pos_y": 0,
                    "world_pos_z": 0,
                    "world_rot_y": 0,
                    "width": 50,
                }
            ]
        }
        resp = self._patch(body)
        self.assertEqual(resp.status_code, 200)
        lf = LayoutFixture.objects.get(layout=self.layout)
        self.assertEqual(lf.width, 50)
        self.assertEqual(lf.height, 200)  # master 값
        self.assertEqual(lf.depth, 300)

    def test_update_size_row_independent(self):
        """같은 master 의 두 LayoutFixture row 중 하나만 width 변경 → 다른 row 영향 X."""
        lf_a = _lf_create(
            layout=self.layout,
            fixture_version=self.version,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
            width=100,
            height=200,
            depth=300,
        )
        lf_b = _lf_create(
            layout=self.layout,
            fixture_version=self.version,
            world_pos_x=500,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
            width=100,
            height=200,
            depth=300,
        )
        body = {
            "fixtures": [
                {
                    "layout_fixture_id": lf_a.id,
                    "fixture_version_id": self.version.id,
                    "world_pos_x": 0,
                    "world_pos_y": 0,
                    "world_pos_z": 0,
                    "world_rot_y": 0,
                    "width": 999,
                },
                {
                    "layout_fixture_id": lf_b.id,
                    "fixture_version_id": self.version.id,
                    "world_pos_x": 500,
                    "world_pos_y": 0,
                    "world_pos_z": 0,
                    "world_rot_y": 0,
                },
            ]
        }
        resp = self._patch(body)
        self.assertEqual(resp.status_code, 200)
        lf_a.refresh_from_db()
        lf_b.refresh_from_db()
        self.master.refresh_from_db()
        self.assertEqual(lf_a.width, 999)
        self.assertEqual(lf_b.width, 100)  # 변경 안 됨 (사이즈 누락)
        self.assertEqual(self.master.width, 100)  # master 영향 X

    def test_invalid_size_zero_returns_422(self):
        """width=0 → INVALID_FIXTURE_DATA 422, DB 변화 0."""
        body = {
            "fixtures": [
                {
                    "fixture_version_id": self.version.id,
                    "world_pos_x": 0,
                    "world_pos_y": 0,
                    "world_pos_z": 0,
                    "world_rot_y": 0,
                    "width": 0,
                }
            ]
        }
        resp = self._patch(body)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_FIXTURE_DATA")
        self.assertFalse(LayoutFixture.objects.filter(layout=self.layout).exists())


class LayoutFixturesCopyEndpointTests(TestCase):
    """POST /api/v1/layouts/{layout_id}/fixtures:copy — 다중 복사 (S14P31F106-240).

    권한: 매장 멤버 누구나 (STAFF 포함).
    검증 실패: INVALID_LAYOUT_FIXTURE_ID 422, DB 변화 0 (all-or-nothing).
    """

    url_tmpl = "/api/v1/layouts/{layout_id}/fixtures:copy"

    def setUp(self):
        self.owner = User.objects.create_user(
            email="copy_owner@example.com",
            password="Pwd1234!",
            name="복사오너",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="copy_staff@example.com",
            password="Pwd1234!",
            name="복사스태프",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="copy_outsider@example.com",
            password="Pwd1234!",
            name="복사외부",
            confirmed=True,
        )
        self.store_a = Store.objects.create(
            user=self.owner,
            name="A매장",
            address="서울",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store_a, user=self.owner, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store_a, user=self.staff, role=StoreMember.Role.STAFF
        )
        self.store_b = Store.objects.create(
            user=self.outsider,
            name="B매장",
            address="부산",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store_b, user=self.outsider, role=StoreMember.Role.MANAGER
        )

        self.layout_a = Layout.objects.create(
            store=self.store_a, name="레이아웃A", is_active=False
        )
        self.layout_b_other_store = Layout.objects.create(
            store=self.store_b, name="레이아웃B(타매장)", is_active=False
        )
        self.master = FixtureMaster.objects.create(
            user=self.owner, name="중앙", width=120, height=90, depth=60
        )
        self.version = FixtureVersion.objects.create(
            fixture_master=self.master, version_name="v1"
        )

        self.lf1 = _lf_create(
            layout=self.layout_a,
            fixture_version=self.version,
            world_pos_x=10,
            world_pos_y=20,
            world_pos_z=30,
            world_rot_y=90,
            width=120,
            height=90,
            depth=60,
        )
        self.lf2 = _lf_create(
            layout=self.layout_a,
            fixture_version=self.version,
            world_pos_x=100,
            world_pos_y=0,
            world_pos_z=200,
            world_rot_y=0,
            width=50,
            height=50,
            depth=50,
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _post(self, layout_id, body, access_token=None):
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url_tmpl.format(layout_id=layout_id),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_pure_copy_success(self):
        """2건 복사 → 새 2 row, 응답 매핑·copied_count 정확."""
        resp = self._post(
            self.layout_a.id,
            {"layout_fixture_ids": [self.lf1.id, self.lf2.id]},
            self.owner_access,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["layout_id"], self.layout_a.id)
        self.assertEqual(data["copied_count"], 2)
        source_ids = [c["source_layout_fixture_id"] for c in data["copied"]]
        self.assertEqual(source_ids, [self.lf1.id, self.lf2.id])
        new_ids = [c["new_layout_fixture_id"] for c in data["copied"]]
        self.assertEqual(len(set(new_ids)), 2)
        self.assertNotIn(self.lf1.id, new_ids)
        self.assertNotIn(self.lf2.id, new_ids)
        # 총 4 row (원본 2 + 복사 2)
        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout_a).count(), 4)

    def test_copy_creates_new_empty_version(self):
        """b-2 패턴: 좌표/회전/사이즈는 원본과 동일하지만 fixture_version 은 새로 생성,
        placement 는 복제 안 됨 (빈 진열대). fixture_master 는 원본 그대로 공유.
        """
        from fixtures.models import FixtureVersionProduct
        from products.models import ProductMaster, ProductVariant, StoreProduct

        # 원본 version 에 placement 시드 (복제 안 됐는지 검증용)
        pm = ProductMaster.objects.create(user=self.owner, width=5, height=5)
        StoreProduct.objects.create(store=self.store_a, product_master=pm)
        variant = ProductVariant.objects.create(product_master=pm, sku_code="SRC")
        FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=variant,
            local_pos_x=1,
            local_pos_y=1,
            local_pos_z=1,
        )

        resp = self._post(
            self.layout_a.id,
            {"layout_fixture_ids": [self.lf1.id]},
            self.owner_access,
        )
        item = resp.json()["data"]["copied"][0]
        new_lf = LayoutFixture.objects.get(id=item["new_layout_fixture_id"])

        # 좌표·사이즈는 원본과 동일
        self.assertEqual(new_lf.world_pos_x, self.lf1.world_pos_x)
        self.assertEqual(new_lf.world_pos_y, self.lf1.world_pos_y)
        self.assertEqual(new_lf.world_pos_z, self.lf1.world_pos_z)
        self.assertEqual(new_lf.world_rot_y, self.lf1.world_rot_y)
        self.assertEqual(new_lf.width, self.lf1.width)
        self.assertEqual(new_lf.height, self.lf1.height)
        self.assertEqual(new_lf.depth, self.lf1.depth)

        # fixture_version 은 새로 생성 (id 다름), 응답에도 포함
        self.assertNotEqual(new_lf.fixture_version_id, self.lf1.fixture_version_id)
        self.assertEqual(item["new_fixture_version_id"], new_lf.fixture_version_id)

        # fixture_master 는 원본 그대로 공유
        self.assertEqual(
            new_lf.fixture_version.fixture_master_id,
            self.lf1.fixture_version.fixture_master_id,
        )

        # version_name 은 원본 + " (사본)"
        self.assertEqual(
            new_lf.fixture_version.version_name,
            f"{self.version.version_name} (사본)",
        )

        # placement 는 복제 안 됨 (새 version 의 placement 0개)
        self.assertEqual(
            FixtureVersionProduct.objects.filter(
                fixture_version=new_lf.fixture_version
            ).count(),
            0,
        )
        # 원본 version 의 placement 는 그대로 유지 (영향 X)
        self.assertEqual(
            FixtureVersionProduct.objects.filter(fixture_version=self.version).count(),
            1,
        )

    def test_empty_array_rejected_by_schema(self):
        """layout_fixture_ids=[] → 422 (Field min_length=1)."""
        resp = self._post(
            self.layout_a.id, {"layout_fixture_ids": []}, self.owner_access
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout_a).count(), 2)

    def test_unknown_layout_fixture_id_rejected(self):
        """미존재 ID 포함 → 422 INVALID_LAYOUT_FIXTURE_ID, DB 변화 0."""
        resp = self._post(
            self.layout_a.id,
            {"layout_fixture_ids": [self.lf1.id, 99999]},
            self.owner_access,
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_LAYOUT_FIXTURE_ID")
        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout_a).count(), 2)

    def test_cross_layout_id_rejected(self):
        """다른 layout 소속 layout_fixture_id 차단 → 422."""
        foreign = _lf_create(
            layout=self.layout_b_other_store,
            fixture_version=self.version,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )
        resp = self._post(
            self.layout_a.id,
            {"layout_fixture_ids": [self.lf1.id, foreign.id]},
            self.owner_access,
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_LAYOUT_FIXTURE_ID")
        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout_a).count(), 2)

    def test_layout_not_found_other_store(self):
        """outsider 가 store_a 의 layout 접근 → 404 LAYOUT_NOT_FOUND."""
        resp = self._post(
            self.layout_a.id,
            {"layout_fixture_ids": [self.lf1.id]},
            self.outsider_access,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "LAYOUT_NOT_FOUND")

    def test_unauthenticated(self):
        """인증 헤더 없이 호출 → 401."""
        resp = self.client.post(
            self.url_tmpl.format(layout_id=self.layout_a.id),
            data=json.dumps({"layout_fixture_ids": [self.lf1.id]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_staff_can_copy(self):
        """STAFF 도 복사 가능 (admin tier 게이트 X)."""
        resp = self._post(
            self.layout_a.id,
            {"layout_fixture_ids": [self.lf1.id]},
            self.staff_access,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["copied_count"], 1)


_FLOORPLAN_PARSE_TEMP_MEDIA = tempfile.mkdtemp(prefix="dogo_layout_parse_test_")


@override_settings(MEDIA_ROOT=_FLOORPLAN_PARSE_TEMP_MEDIA)
class LayoutFloorplanParseEndpointTests(TestCase):
    """POST /api/v1/layouts/{layout_id}/floorplan/parse.

    한 호출로 도면 저장 + FixtureMaster N + FixtureVersion N + LayoutFixture N
    자동 생성. ai-worker celery task 는 mocking 으로 격리.

    배포 환경 검증 포인트:
      - 권한 게이트 (ADMIN_ROLES — STAFF 거부, 비회원 거부)
      - 파일 검증 (415 / 413)
      - ai-worker 실패 시 PARSE_FAILED 500 + 전체 롤백 (DB 변화 0)
      - 좌표 변환 (도면 픽셀 → store.width/depth 기준 cm)
      - Layout.floorplan_image_url 갱신
    """

    url_tmpl = "/api/v1/layouts/{layout_id}/floorplan/parse"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_FLOORPLAN_PARSE_TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(
            email="parse_owner@example.com",
            password="X1!abcde",
            name="오너",
            confirmed=True,
        )
        self.manager = User.objects.create_user(
            email="parse_manager@example.com",
            password="X1!abcde",
            name="매니저",
            confirmed=True,
        )
        self.vmd = User.objects.create_user(
            email="parse_vmd@example.com",
            password="X1!abcde",
            name="VMD",
            confirmed=True,
        )
        self.staff = User.objects.create_user(
            email="parse_staff@example.com",
            password="X1!abcde",
            name="스태프",
            confirmed=True,
        )
        self.outsider = User.objects.create_user(
            email="parse_out@example.com",
            password="X1!abcde",
            name="외부",
            confirmed=True,
        )

        # 매장 사이즈 (cm): width=2000, depth=1000, height(천장)=1000.
        # 사용자 합의된 기본값과 동일 — scaling 검증 시 픽셀×scale 매핑이
        # 정수로 떨어지도록 image_width=2000, image_height=1000 으로도 잡음.
        self.store = Store.objects.create(
            user=self.owner,
            name="매장 A",
            address="서울 성동구",
            width=2000,
            height=1000,
            depth=1000,
        )
        StoreMember.objects.create(
            store=self.store, user=self.owner, role=StoreMember.Role.OWNER
        )
        StoreMember.objects.create(
            store=self.store, user=self.manager, role=StoreMember.Role.MANAGER
        )
        StoreMember.objects.create(
            store=self.store, user=self.vmd, role=StoreMember.Role.VMD
        )
        StoreMember.objects.create(
            store=self.store, user=self.staff, role=StoreMember.Role.STAFF
        )

        self.layout = Layout.objects.create(
            store=self.store,
            name="새 레이아웃",
            comment="도면 분석 대상",
        )

        self.owner_access = str(AccessToken.for_user(self.owner))
        self.manager_access = str(AccessToken.for_user(self.manager))
        self.vmd_access = str(AccessToken.for_user(self.vmd))
        self.staff_access = str(AccessToken.for_user(self.staff))
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _png(self, name="floor.png", size=128):
        return SimpleUploadedFile(
            name,
            b"\x89PNG\r\n\x1a\n" + b"\x00" * size,
            content_type="image/png",
        )

    def _post(self, layout_id, file=None, access_token=None):
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        data = {}
        if file is not None:
            data["file"] = file
        return self.client.post(
            self.url_tmpl.format(layout_id=layout_id),
            data=data,
        )

    # ── 성공 ───────────────────────────────────────────────────────────

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_success_manager_parses_and_creates(self, mock_parse):
        """MANAGER 가 도면 분석 → Layout 갱신 + Fixture* 3종 INSERT."""
        mock_parse.return_value = {
            "image_width": 2000,
            "image_height": 1000,
            "fixtures": [
                {"x": 100, "y": 200, "width": 50, "height": 60, "rotation": 0},
                {"x": 300, "y": 400, "width": 80, "height": 40, "rotation": 90},
            ],
        }
        response = self._post(
            self.layout.id, file=self._png(), access_token=self.manager_access
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "도면 이미지가 성공적으로 분석되었습니다.")

        data = body["data"]
        # 응답 키 잠금
        self.assertEqual(
            set(data.keys()),
            {
                "layout_id",
                "store_id",
                "name",
                "comment",
                "is_active",
                "floorplan_image_url",
                "store_dimensions",
                "fixtures",
                "parsed_at",
            },
        )
        self.assertEqual(data["layout_id"], self.layout.id)
        self.assertEqual(data["store_id"], self.store.id)
        self.assertEqual(data["name"], "새 레이아웃")
        self.assertTrue(data["floorplan_image_url"].startswith("/media/"))
        self.assertTrue(
            data["floorplan_image_url"].endswith(".png"),
            data["floorplan_image_url"],
        )
        self.assertEqual(len(data["fixtures"]), 2)

        # Layout 갱신 확인
        self.layout.refresh_from_db()
        self.assertEqual(self.layout.floorplan_image_url, data["floorplan_image_url"])

        # FixtureMaster 2개 (user=manager, name="집기 1/2", height=500cm)
        masters = list(FixtureMaster.objects.filter(user=self.manager).order_by("id"))
        self.assertEqual(len(masters), 2)
        self.assertEqual(masters[0].name, "집기 1")
        self.assertEqual(masters[1].name, "집기 2")
        for m in masters:
            self.assertEqual(m.height, 500)

        # FixtureVersion 2개 (각 master 당 "진열 1")
        versions = list(FixtureVersion.objects.order_by("id"))
        self.assertEqual(len(versions), 2)
        for v in versions:
            self.assertEqual(v.version_name, "진열 1")

        # LayoutFixture 2개
        lfs = list(LayoutFixture.objects.filter(layout=self.layout).order_by("id"))
        self.assertEqual(len(lfs), 2)

        # 좌표 변환 검증 (image=2000×1000, store=2000×1000 → scale 1.0)
        # fixture 0: x=100, y=200, w=50, h=60
        # world_pos_x = (100 + 25) * 1 = 125
        # world_pos_z = (200 + 30) * 1 = 230
        self.assertEqual(lfs[0].world_pos_x, 125)
        self.assertEqual(lfs[0].world_pos_y, 0)
        self.assertEqual(lfs[0].world_pos_z, 230)
        self.assertEqual(lfs[0].world_rot_y, 0)
        self.assertEqual(lfs[0].width, 50)
        self.assertEqual(lfs[0].depth, 60)
        self.assertEqual(lfs[0].height, 500)

        # fixture 1: x=300, y=400, w=80, h=40, rotation=90
        # world_pos_x = (300 + 40) * 1 = 340
        # world_pos_z = (400 + 20) * 1 = 420
        self.assertEqual(lfs[1].world_pos_x, 340)
        self.assertEqual(lfs[1].world_pos_z, 420)
        self.assertEqual(lfs[1].world_rot_y, 90)

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_success_vmd_also_admin(self, mock_parse):
        """VMD 도 admin tier — parse 호출 가능."""
        mock_parse.return_value = {
            "image_width": 2000,
            "image_height": 1000,
            "fixtures": [],
        }
        response = self._post(
            self.layout.id, file=self._png(), access_token=self.vmd_access
        )
        self.assertEqual(response.status_code, 200)

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_success_zero_fixtures(self, mock_parse):
        """검출 결과 0개도 정상 — Layout 갱신만 + fixtures=[]."""
        mock_parse.return_value = {
            "image_width": 1920,
            "image_height": 1080,
            "fixtures": [],
        }
        response = self._post(
            self.layout.id, file=self._png(), access_token=self.manager_access
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["fixtures"], [])
        self.layout.refresh_from_db()
        self.assertIsNotNone(self.layout.floorplan_image_url)
        self.assertEqual(LayoutFixture.objects.filter(layout=self.layout).count(), 0)

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_scaling_with_non_unit_ratio(self, mock_parse):
        """image_width=1000, store.width=2000 → scale_x=2.0 정확히 적용."""
        mock_parse.return_value = {
            "image_width": 1000,
            "image_height": 500,
            "fixtures": [
                {"x": 100, "y": 50, "width": 20, "height": 30, "rotation": 0},
            ],
        }
        response = self._post(
            self.layout.id, file=self._png(), access_token=self.manager_access
        )
        self.assertEqual(response.status_code, 200)
        lf = LayoutFixture.objects.get(layout=self.layout)
        # scale_x = 2000/1000 = 2.0, scale_z = 1000/500 = 2.0
        # master: width=20*2=40, depth=30*2=60
        # world_pos_x = (100 + 10) * 2 = 220
        # world_pos_z = (50 + 15) * 2 = 130
        self.assertEqual(lf.width, 40)
        self.assertEqual(lf.depth, 60)
        self.assertEqual(lf.world_pos_x, 220)
        self.assertEqual(lf.world_pos_z, 130)

    # ── 인증/권한 ──────────────────────────────────────────────────────

    def test_unauthorized_no_cookie(self):
        response = self._post(self.layout.id, file=self._png())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_forbidden_staff(self, mock_parse):
        """STAFF 는 admin tier 외 — 403 FORBIDDEN_ACCESS."""
        response = self._post(
            self.layout.id, file=self._png(), access_token=self.staff_access
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "FORBIDDEN_ACCESS")
        mock_parse.assert_not_called()

    def test_outsider_layout_not_found(self):
        """비매장 멤버는 404 (ID 프로빙 방지)."""
        response = self._post(
            self.layout.id, file=self._png(), access_token=self.outsider_access
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    def test_layout_not_found_missing_id(self):
        response = self._post(
            999_999, file=self._png(), access_token=self.manager_access
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "LAYOUT_NOT_FOUND")

    # ── 파일 검증 ──────────────────────────────────────────────────────

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_unsupported_media_type(self, mock_parse):
        bad = SimpleUploadedFile(
            "floor.gif", b"GIF89a" + b"\x00" * 32, content_type="image/gif"
        )
        response = self._post(
            self.layout.id, file=bad, access_token=self.manager_access
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "UNSUPPORTED_MEDIA_TYPE")
        mock_parse.assert_not_called()

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_payload_too_large(self, mock_parse):
        big = SimpleUploadedFile(
            "huge.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024),
            content_type="image/png",
        )
        response = self._post(
            self.layout.id, file=big, access_token=self.manager_access
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "PAYLOAD_TOO_LARGE")
        mock_parse.assert_not_called()

    # ── ai-worker 실패 + 롤백 ────────────────────────────────────────

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_ai_worker_exception_rolls_back(self, mock_parse):
        """ai-worker 가 예외 던지면 PARSE_FAILED 500 + DB 변화 0."""
        mock_parse.side_effect = TimeoutError("celery get timeout")

        master_count_before = FixtureMaster.objects.count()
        version_count_before = FixtureVersion.objects.count()
        lf_count_before = LayoutFixture.objects.count()

        response = self._post(
            self.layout.id, file=self._png(), access_token=self.manager_access
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "PARSE_FAILED")

        # 전체 롤백 — DB 변화 0
        self.assertEqual(FixtureMaster.objects.count(), master_count_before)
        self.assertEqual(FixtureVersion.objects.count(), version_count_before)
        self.assertEqual(LayoutFixture.objects.count(), lf_count_before)
        self.layout.refresh_from_db()
        self.assertIsNone(self.layout.floorplan_image_url)

    @patch("layouts.services._call_ai_floorplan_parse")
    def test_ai_worker_invalid_response_rolls_back(self, mock_parse):
        """ai-worker 응답 image_width=0 → PARSE_FAILED + 롤백."""
        mock_parse.return_value = {
            "image_width": 0,
            "image_height": 0,
            "fixtures": [],
        }

        response = self._post(
            self.layout.id, file=self._png(), access_token=self.manager_access
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "PARSE_FAILED")
        self.layout.refresh_from_db()
        self.assertIsNone(self.layout.floorplan_image_url)
