from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from ninja_jwt.tokens import AccessToken

from fixtures.models import FixtureMaster, FixtureVersion, FixtureVersionProduct
from layouts.models import Layout, LayoutFixture
from products.models import ProductMaster, ProductVariant, StoreProduct
from stores.models import Store, StoreMember
from users.models import User


def _lf_create(*, layout, fixture_version, **kwargs):
    """LayoutFixture 시드 헬퍼. width/height/depth 명시 안 하면 placeholder
    값(10) 자동 — S14P31F106-240 의 NOT NULL 컬럼 추가로 필수."""
    kwargs.setdefault("width", 10)
    kwargs.setdefault("height", 10)
    kwargs.setdefault("depth", 10)
    return LayoutFixture.objects.create(
        layout=layout, fixture_version=fixture_version, **kwargs
    )


def _make_store(user, name="매장", role=StoreMember.Role.MANAGER):
    """매장 + 멤버십 한 쌍 생성. user 가 role 로 가입."""
    store = Store.objects.create(
        user=user,
        name=name,
        address="서울",
        max_admin_count=5,
        width=1000,
        height=300,
        depth=800,
    )
    StoreMember.objects.create(store=store, user=user, role=role)
    return store


def _add_member(store, user, role=StoreMember.Role.STAFF):
    """기존 매장에 user 멤버로 추가. fellow visibility 테스트용."""
    StoreMember.objects.create(store=store, user=user, role=role)


def _make_fixture(user, name="중앙 매대", width=120, height=90, depth=60):
    return FixtureMaster.objects.create(
        user=user,
        name=name,
        width=width,
        height=height,
        depth=depth,
    )


class FixturesListEndpointTests(TestCase):
    """GET /api/v1/fixtures — 매장 단위 공유 정책.

    가시성: 본인 + 본인이 속한 매장의 다른 멤버가 등록한 fixture (alive only).
    명세 amend 됨 — API2 spec line 16~26 참조.
    """

    url = "/api/v1/fixtures"

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _get(self, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url, **headers)

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        response = self._get()
        self.assertEqual(response.status_code, 401)

    # ── 빈 케이스 ─────────────────────────────────────────────────────

    def test_user_with_no_store_membership_returns_empty(self):
        """매장 멤버십 없는 사용자는 fixture 가시 범위 = 자기 자신뿐인데
        StoreMember 0건이라 fellow_user_ids 도 0건 → empty list."""
        response = self._get(self.alice_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "전체 집기 목록 조회에 성공했습니다.")
        self.assertEqual(body["data"], {"fixtures": []})

    def test_no_fixtures_in_store_returns_empty(self):
        """매장 멤버지만 fixture 등록자 0명인 경우 empty list."""
        _make_store(self.alice)
        response = self._get(self.alice_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"fixtures": []})

    # ── 가시성 — 본인 fixture ─────────────────────────────────────────

    def test_user_sees_own_fixtures(self):
        _make_store(self.alice)  # alice 가 MANAGER 로 멤버십 가짐
        f = _make_fixture(self.alice, name="앨리스 매대")
        response = self._get(self.alice_access)
        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["fixtures"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fixture_id"], f.id)
        self.assertEqual(items[0]["name"], "앨리스 매대")
        self.assertEqual(items[0]["width"], 120)
        self.assertEqual(items[0]["height"], 90)
        self.assertEqual(items[0]["depth"], 60)
        self.assertIn("created_at", items[0])

    # ── 가시성 — 동료 fixture 공유 ─────────────────────────────────────

    def test_fellow_member_sees_my_fixtures_and_vice_versa(self):
        """alice + bob 같은 매장 멤버. alice 의 fixture 는 bob 에게도 보임."""
        store = _make_store(self.alice)
        _add_member(store, self.bob)

        f_alice = _make_fixture(self.alice, name="앨리스 매대")
        f_bob = _make_fixture(self.bob, name="밥 행거")

        # bob 시점
        response = self._get(self.bob_access)
        items = response.json()["data"]["fixtures"]
        ids = {item["fixture_id"] for item in items}
        self.assertSetEqual(ids, {f_alice.id, f_bob.id})

        # alice 시점도 동일
        response = self._get(self.alice_access)
        ids = {item["fixture_id"] for item in response.json()["data"]["fixtures"]}
        self.assertSetEqual(ids, {f_alice.id, f_bob.id})

    # ── 가시성 — 다른 매장 사용자의 fixture 차단 ──────────────────────

    def test_other_store_user_fixture_invisible(self):
        """alice 와 bob 이 서로 다른 매장 멤버 → 서로의 fixture 안 보임."""
        _make_store(self.alice, name="앨리스의 매장")
        _make_store(self.bob, name="밥의 매장")

        f_alice = _make_fixture(self.alice, name="앨리스 fixture")
        f_bob = _make_fixture(self.bob, name="밥 fixture")

        response = self._get(self.alice_access)
        ids = {item["fixture_id"] for item in response.json()["data"]["fixtures"]}
        self.assertSetEqual(ids, {f_alice.id})
        self.assertNotIn(f_bob.id, ids)

        response = self._get(self.bob_access)
        ids = {item["fixture_id"] for item in response.json()["data"]["fixtures"]}
        self.assertSetEqual(ids, {f_bob.id})
        self.assertNotIn(f_alice.id, ids)

    # ── 다중 매장 hub 시나리오 ────────────────────────────────────────

    def test_multi_store_hub_user_propagates_visibility_partially(self):
        """alice 가 store X + Y 양쪽 멤버. bob 은 X 만, carol 은 Y 만.
        alice 의 fixture 는 bob, carol 모두에게 보임 (각자 alice 와 store 공유).
        bob 과 carol 은 서로 store 안 겹침 → bob, carol 의 fixture 는 서로 안 보임.
        """
        carol = User.objects.create_user(
            email="carol@example.com",
            password="Pwd1234!",
            name="캐롤",
            confirmed=True,
        )
        carol_access = str(AccessToken.for_user(carol))

        store_x = _make_store(self.alice, name="X 매장")
        store_y = Store.objects.create(
            user=self.alice,
            name="Y 매장",
            address="부산",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=store_y, user=self.alice, role=StoreMember.Role.MANAGER
        )
        _add_member(store_x, self.bob)
        _add_member(store_y, carol)

        f_alice = _make_fixture(self.alice, name="alice fixture")
        f_bob = _make_fixture(self.bob, name="bob fixture")
        f_carol = _make_fixture(carol, name="carol fixture")

        # bob 시점: 자기 + alice (store X 공유). carol 안 보임.
        ids = {
            item["fixture_id"]
            for item in self._get(self.bob_access).json()["data"]["fixtures"]
        }
        self.assertSetEqual(ids, {f_alice.id, f_bob.id})

        # carol 시점: 자기 + alice (store Y 공유). bob 안 보임.
        ids = {
            item["fixture_id"]
            for item in self._get(carol_access).json()["data"]["fixtures"]
        }
        self.assertSetEqual(ids, {f_alice.id, f_carol.id})

        # alice 시점: 셋 다 보임 (각각 본인이 멤버인 store 의 다른 멤버).
        ids = {
            item["fixture_id"]
            for item in self._get(self.alice_access).json()["data"]["fixtures"]
        }
        self.assertSetEqual(ids, {f_alice.id, f_bob.id, f_carol.id})

    # ── soft-delete 제외 ──────────────────────────────────────────────

    def test_soft_deleted_fixture_excluded(self):
        _make_store(self.alice)
        f_alive = _make_fixture(self.alice, name="살아있는 fixture")
        f_dead = _make_fixture(self.alice, name="삭제된 fixture")
        f_dead.deleted_at = timezone.now()
        f_dead.save(update_fields=["deleted_at"])

        response = self._get(self.alice_access)
        ids = {item["fixture_id"] for item in response.json()["data"]["fixtures"]}
        self.assertSetEqual(ids, {f_alive.id})
        self.assertNotIn(f_dead.id, ids)

    # ── 정렬 ──────────────────────────────────────────────────────────

    def test_ordered_by_created_at_ascending(self):
        """스펙 example 순서대로 등록 시간 오름차순."""
        _make_store(self.alice)
        f1 = _make_fixture(self.alice, name="1번")
        f2 = _make_fixture(self.alice, name="2번")
        f3 = _make_fixture(self.alice, name="3번")

        response = self._get(self.alice_access)
        order = [item["fixture_id"] for item in response.json()["data"]["fixtures"]]
        self.assertEqual(order, [f1.id, f2.id, f3.id])

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        _make_fixture(self.alice)
        body = self._get(self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        self.assertIn("fixtures", body["data"])
        keys = set(body["data"]["fixtures"][0].keys())
        self.assertSetEqual(
            keys,
            {"fixture_id", "name", "width", "height", "depth", "created_at"},
        )


class FixtureCreateEndpointTests(TestCase):
    """POST /api/v1/fixtures — 매장 멤버 1+ 만 등록 가능.

    매장에 소속되지 않은 사용자는 fixture 만들어도 본인 외 아무도 못 봐서 무의미.
    FORBIDDEN_NO_STORE 403 으로 차단. 명세 amend 됨 — API2 spec 참고.
    """

    url = "/api/v1/fixtures"

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))

        # 매장 멤버 없는 외부인 — FORBIDDEN_NO_STORE 검증용
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            password="Pwd1234!",
            name="외부인",
            confirmed=True,
        )
        self.outsider_access = str(AccessToken.for_user(self.outsider))

    def _post(self, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            **headers,
        )

    def _payload(self, **overrides):
        body = {"name": "2단 철제 선반", "width": 120, "height": 150, "depth": 40}
        body.update(overrides)
        return body

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        response = self._post(self._payload())
        self.assertEqual(response.status_code, 401)

    # ── 권한 — 매장 멤버 0건 차단 ─────────────────────────────────────

    def test_outsider_without_store_membership_forbidden(self):
        """매장 무소속 사용자는 FORBIDDEN_NO_STORE 403."""
        response = self._post(self._payload(), self.outsider_access)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "FORBIDDEN_NO_STORE")
        # row 가 만들어지지 않았어야 함 (검증 실패 시 INSERT 차단)
        self.assertEqual(FixtureMaster.objects.count(), 0)

    # ── 성공 ───────────────────────────────────────────────────────────

    def test_success_creates_fixture_with_user_fk(self):
        _make_store(self.alice)
        response = self._post(self._payload(), self.alice_access)
        self.assertEqual(response.status_code, 200)

        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "새로운 집기가 성공적으로 등록되었습니다.")

        data = body["data"]
        self.assertIn("fixture_id", data)
        self.assertEqual(data["name"], "2단 철제 선반")
        self.assertEqual(data["width"], 120)
        self.assertEqual(data["height"], 150)
        self.assertEqual(data["depth"], 40)
        self.assertIn("created_at", data)

        # DB 검증 — user FK + alive
        fixture = FixtureMaster.objects.get(id=data["fixture_id"])
        self.assertEqual(fixture.user_id, self.alice.id)
        self.assertIsNone(fixture.deleted_at)

    def test_user_with_multiple_store_memberships_can_create(self):
        """매장 2개 멤버여도 정상 — exists() 검증이라 카운트 무관."""
        _make_store(self.alice, name="X 매장")
        _make_store(self.alice, name="Y 매장")
        response = self._post(self._payload(), self.alice_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FixtureMaster.objects.filter(user=self.alice).count(), 1)

    # ── 입력 검증 ─────────────────────────────────────────────────────

    def test_missing_required_fields_returns_validation_error(self):
        """name 누락 시 422 (django-ninja schema validation)."""
        _make_store(self.alice)
        response = self._post(
            {"width": 120, "height": 150, "depth": 40},  # name 누락
            self.alice_access,
        )
        self.assertEqual(response.status_code, 422)
        # row 미생성
        self.assertEqual(FixtureMaster.objects.count(), 0)

    def test_wrong_type_returns_validation_error(self):
        """width 가 string 이면 422."""
        _make_store(self.alice)
        response = self._post(
            {"name": "테스트", "width": "abc", "height": 150, "depth": 40},
            self.alice_access,
        )
        self.assertEqual(response.status_code, 422)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        body = self._post(self._payload(), self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        keys = set(body["data"].keys())
        self.assertSetEqual(
            keys,
            {"fixture_id", "name", "width", "height", "depth", "created_at"},
        )


class FixtureDetailEndpointTests(TestCase):
    """GET /api/v1/fixtures/{fixture_id} — 매장 단위 공유 정책 + asset_3d join.

    list 와 동일한 가시성. 비가시/미존재/soft-deleted 모두 FIXTURE_NOT_FOUND 404.
    asset_3d 는 nullable — Asset3D row 가 있으면 최신 1건 채택.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _get(self, fixture_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(f"/api/v1/fixtures/{fixture_id}", **headers)

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._get(f.id)
        self.assertEqual(response.status_code, 401)

    # ── 가시성 — 본인 / 동료 / 다른 매장 ─────────────────────────────

    def test_owner_sees_own_fixture(self):
        _make_store(self.alice)
        f = _make_fixture(
            self.alice, name="앨리스 매대", width=120, height=90, depth=60
        )
        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertEqual(data["fixture_id"], f.id)
        self.assertEqual(data["name"], "앨리스 매대")
        self.assertEqual(data["dimensions"], {"width": 120, "height": 90, "depth": 60})
        self.assertIsNone(data["asset_3d"])
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)

    def test_fellow_member_sees_my_fixture(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob)
        f = _make_fixture(self.alice)
        response = self._get(f.id, self.bob_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["fixture_id"], f.id)

    def test_other_store_user_returns_not_found(self):
        """다른 매장 사용자가 알지 못하는 ID 로 조회 → FIXTURE_NOT_FOUND 404
        (ID 프로빙 차단 — 토큰 존재성 노출 X)."""
        _make_store(self.alice, name="앨리스 매장")
        _make_store(self.bob, name="밥 매장")
        f_alice = _make_fixture(self.alice)
        response = self._get(f_alice.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_user_with_no_store_membership_returns_not_found(self):
        """매장 멤버십 0건 사용자는 본인이 만든 거라도 안 보임 — 정합성 게이트."""
        f = _make_fixture(self.alice)  # alice 멤버십 0
        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    # ── 미존재 / soft-delete ──────────────────────────────────────────

    def test_nonexistent_id_returns_not_found(self):
        _make_store(self.alice)
        response = self._get(999_999, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_soft_deleted_fixture_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        f.deleted_at = timezone.now()
        f.save(update_fields=["deleted_at"])

        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    # ── asset_3d join ────────────────────────────────────────────────

    def test_asset_3d_present_when_registered(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from assets_3d.models import Asset3D

        _make_store(self.alice)
        f = _make_fixture(self.alice)
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=f.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url=SimpleUploadedFile(
                "m.glb", b"x", content_type="model/gltf-binary"
            ),
            file_size_bytes=2048,
        )

        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        asset = response.json()["data"]["asset_3d"]
        self.assertIsNotNone(asset)
        self.assertEqual(asset["file_format"], "GLB")
        self.assertEqual(asset["file_size"], 2048)
        self.assertIn("model_url", asset)
        self.assertTrue(asset["model_url"])  # non-empty path

    def test_asset_3d_picks_latest_when_multiple(self):
        """같은 fixture 에 Asset3D row 가 여러 개면 created_at 최신 1개만 채택."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from assets_3d.models import Asset3D

        _make_store(self.alice)
        f = _make_fixture(self.alice)
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=f.id,
            file_format=Asset3D.FileFormat.OBJ,
            model_url=SimpleUploadedFile("old.obj", b"x"),
            file_size_bytes=1000,
        )
        latest = Asset3D.objects.create(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=f.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url=SimpleUploadedFile("new.glb", b"y"),
            file_size_bytes=2000,
        )

        response = self._get(f.id, self.alice_access)
        asset = response.json()["data"]["asset_3d"]
        self.assertEqual(asset["file_format"], latest.file_format)
        self.assertEqual(asset["file_size"], 2000)

    def test_asset_3d_filtered_by_target_type(self):
        """target_type=FIXTURE 만 join — PRODUCT/STORE 의 동일 target_id 는 무시."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from assets_3d.models import Asset3D

        _make_store(self.alice)
        f = _make_fixture(self.alice)
        # PRODUCT 타입 — fixture 와 같은 ID 라도 무시되어야 함
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=f.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url=SimpleUploadedFile("p.glb", b"x"),
        )

        response = self._get(f.id, self.alice_access)
        self.assertIsNone(response.json()["data"]["asset_3d"])

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        body = self._get(f.id, self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        keys = set(body["data"].keys())
        self.assertSetEqual(
            keys,
            {
                "fixture_id",
                "name",
                "dimensions",
                "asset_3d",
                "created_at",
                "updated_at",
            },
        )
        dim_keys = set(body["data"]["dimensions"].keys())
        self.assertSetEqual(dim_keys, {"width", "height", "depth"})


def _seed_alive_placement(fixture, *, user=None):
    """fixture 위에 alive version + placement 1건 시드 — FIXTURE_IS_NOT_EMPTY 검증용.

    user 미지정 시 fixture.user 가 ProductMaster/Variant 의 owner.
    """
    owner = user or fixture.user
    pm = ProductMaster.objects.create(user=owner, width=10, height=10)
    variant = ProductVariant.objects.create(product_master=pm)
    version = FixtureVersion.objects.create(fixture_master=fixture, version_name="v1")
    return FixtureVersionProduct.objects.create(
        fixture_version=version,
        variant=variant,
        local_pos_x=0,
        local_pos_y=0,
        local_pos_z=0,
    )


class FixtureUpdateEndpointTests(TestCase):
    """PATCH /api/v1/fixtures/{fixture_id} — admin-tier 전용, 크기 변경 시 placement lock.

    가시성: list/detail 와 동일. 비가시/미존재/soft-deleted 모두 FIXTURE_NOT_FOUND 404.
    추가: STAFF / non-admin 도 같은 코드 404 로 묶음 (spec line 20, ID 프로빙 차단).
    크기(width/height/depth) 변경 시 alive version 의 placement 가 1+ 면 FIXTURE_IS_NOT_EMPTY.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _patch(self, fixture_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            f"/api/v1/fixtures/{fixture_id}",
            data=body,
            content_type="application/json",
            **headers,
        )

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._patch(f.id, {"name": "새 이름"})
        self.assertEqual(response.status_code, 401)

    # ── 가시성 / admin-tier 게이트 — 모두 404 로 묶음 ───────────────────

    def test_no_store_membership_returns_not_found(self):
        f = _make_fixture(self.alice)  # alice 멤버십 0
        response = self._patch(f.id, {"name": "새 이름"}, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_other_store_user_returns_not_found(self):
        _make_store(self.alice, name="앨리스 매장")
        _make_store(self.bob, name="밥 매장")
        f = _make_fixture(self.alice)
        response = self._patch(f.id, {"name": "새 이름"}, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_nonexistent_id_returns_not_found(self):
        _make_store(self.alice)
        response = self._patch(999_999, {"name": "x"}, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_soft_deleted_fixture_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        f.deleted_at = timezone.now()
        f.save(update_fields=["deleted_at"])
        response = self._patch(f.id, {"name": "x"}, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_staff_in_same_store_returns_not_found(self):
        """STAFF 는 가시 범위지만 admin 아님 → 404 (spec: STAFF 도 NOT_FOUND 묶음)."""
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.STAFF)
        f = _make_fixture(self.alice, name="원래")
        response = self._patch(f.id, {"name": "변경시도"}, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")
        # row 변경 안 됐어야 함
        f.refresh_from_db()
        self.assertEqual(f.name, "원래")

    # ── admin role 통과 케이스 ─────────────────────────────────────────

    def test_creator_manager_can_update(self):
        _make_store(self.alice)  # alice 가 MANAGER
        f = _make_fixture(self.alice, name="원래")
        response = self._patch(f.id, {"name": "변경"}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "집기 정보가 성공적으로 수정되었습니다.")
        self.assertEqual(body["data"]["name"], "변경")

    def test_fellow_manager_can_update(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.MANAGER)
        f = _make_fixture(self.alice, name="원래")
        response = self._patch(f.id, {"name": "변경"}, self.bob_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.name, "변경")

    def test_fellow_vice_manager_can_update(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.VICE_MANAGER)
        f = _make_fixture(self.alice)
        response = self._patch(f.id, {"name": "변경"}, self.bob_access)
        self.assertEqual(response.status_code, 200)

    def test_fellow_vmd_can_update(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.VMD)
        f = _make_fixture(self.alice)
        response = self._patch(f.id, {"name": "변경"}, self.bob_access)
        self.assertEqual(response.status_code, 200)

    def test_multi_store_hub_admin_in_one_passes(self):
        """user 가 X 에서는 STAFF 지만 Y 에서는 MANAGER. creator(alice) 도 Y 멤버.
        → Y 매장 공유 + bob 의 Y role=MANAGER → 통과."""
        store_x = _make_store(self.alice, name="X")
        store_y = Store.objects.create(
            user=self.alice,
            name="Y",
            address="부산",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=store_y, user=self.alice, role=StoreMember.Role.MANAGER
        )
        _add_member(store_x, self.bob, role=StoreMember.Role.STAFF)
        StoreMember.objects.create(
            store=store_y, user=self.bob, role=StoreMember.Role.MANAGER
        )
        f = _make_fixture(self.alice)
        response = self._patch(f.id, {"name": "hub-edit"}, self.bob_access)
        self.assertEqual(response.status_code, 200)

    # ── 부분 수정 동작 ─────────────────────────────────────────────────

    def test_update_name_only_preserves_dimensions(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice, name="이전", width=100, height=80, depth=50)
        response = self._patch(f.id, {"name": "새 이름"}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.name, "새 이름")
        self.assertEqual(f.width, 100)
        self.assertEqual(f.height, 80)
        self.assertEqual(f.depth, 50)

    def test_update_width_only_preserves_others(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice, name="원래", width=100, height=80, depth=50)
        response = self._patch(f.id, {"width": 150}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.name, "원래")
        self.assertEqual(f.width, 150)
        self.assertEqual(f.height, 80)
        self.assertEqual(f.depth, 50)

    def test_update_all_fields(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice, name="이전", width=100, height=80, depth=50)
        response = self._patch(
            f.id,
            {"name": "새", "width": 150, "height": 180, "depth": 50},
            self.alice_access,
        )
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual((f.name, f.width, f.height, f.depth), ("새", 150, 180, 50))

    def test_empty_body_is_no_op(self):
        """빈 body — save 스킵, updated_at 보호."""
        _make_store(self.alice)
        f = _make_fixture(self.alice, name="원래", width=100, height=80, depth=50)
        original_updated_at = f.updated_at
        response = self._patch(f.id, {}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.name, "원래")
        self.assertEqual(f.updated_at, original_updated_at)

    # ── 크기 변경 lock (FIXTURE_IS_NOT_EMPTY) ──────────────────────────

    def test_width_change_with_placement_blocked(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice, width=100)
        _seed_alive_placement(f)
        response = self._patch(f.id, {"width": 150}, self.alice_access)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "FIXTURE_IS_NOT_EMPTY")
        # row 변경 안 됐어야 함
        f.refresh_from_db()
        self.assertEqual(f.width, 100)

    def test_height_change_with_placement_blocked(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice, height=80)
        _seed_alive_placement(f)
        response = self._patch(f.id, {"height": 90}, self.alice_access)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "FIXTURE_IS_NOT_EMPTY")

    def test_depth_change_with_placement_blocked(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice, depth=50)
        _seed_alive_placement(f)
        response = self._patch(f.id, {"depth": 70}, self.alice_access)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "FIXTURE_IS_NOT_EMPTY")

    def test_name_only_with_placement_allowed(self):
        """name 만 변경하면 placement 있어도 통과 — spec 허용."""
        _make_store(self.alice)
        f = _make_fixture(self.alice, name="원래")
        _seed_alive_placement(f)
        response = self._patch(f.id, {"name": "변경"}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.name, "변경")

    def test_dimension_no_op_value_with_placement_allowed(self):
        """width 같은 값으로 PATCH — 실제 변경 없으니 placement 검증 스킵 → 통과."""
        _make_store(self.alice)
        f = _make_fixture(self.alice, width=100)
        _seed_alive_placement(f)
        response = self._patch(f.id, {"width": 100}, self.alice_access)
        self.assertEqual(response.status_code, 200)

    def test_dimension_change_with_only_soft_deleted_version_placement_allowed(self):
        """soft-deleted version 의 placement 는 차단 대상 아님 (이미 비활성 시안)."""
        _make_store(self.alice)
        f = _make_fixture(self.alice, width=100)
        placement = _seed_alive_placement(f)
        version = placement.fixture_version
        version.deleted_at = timezone.now()
        version.save(update_fields=["deleted_at"])

        response = self._patch(f.id, {"width": 200}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.width, 200)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        body = self._patch(f.id, {"name": "변경"}, self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        keys = set(body["data"].keys())
        self.assertSetEqual(
            keys,
            {"fixture_id", "name", "width", "height", "depth", "updated_at"},
        )


def _attach_fixture_to_layout(layout, fixture, *, version_name="v-attached"):
    """layout 에 fixture 를 batch — FixtureVersion + LayoutFixture row 생성.

    FIXTURE_IN_USE 검증용. 호출자가 layout / version 을 사후에 soft-delete 하면
    cascade 정책 ('alive 만 차단') 검증 가능.
    """
    version = FixtureVersion.objects.create(
        fixture_master=fixture, version_name=version_name
    )
    _lf_create(
        layout=layout,
        fixture_version=version,
        world_pos_x=0,
        world_pos_y=0,
        world_pos_z=0,
        world_rot_y=0,
    )
    return version


class FixtureDeleteEndpointTests(TestCase):
    """DELETE /api/v1/fixtures/{fixture_id} — admin-tier 만, layout 사용 중이면 차단.

    soft-delete: fixture_masters.deleted_at + alive FixtureVersion cascade soft-delete.
    이미 soft-deleted 된 fixture 의 두 번째 DELETE 는 가시성 게이트의 .alive() 에서
    걸러져 FIXTURE_NOT_FOUND 404 (idempotent NOT — 명세 line 75 메시지 정합).
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _delete(self, fixture_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.delete(f"/api/v1/fixtures/{fixture_id}", **headers)

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._delete(f.id)
        self.assertEqual(response.status_code, 401)

    # ── 가시성 / admin-tier 게이트 — 모두 404 묶음 ──────────────────────

    def test_no_store_membership_returns_not_found(self):
        f = _make_fixture(self.alice)  # alice 멤버십 0
        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_other_store_user_returns_not_found(self):
        _make_store(self.alice, name="앨리스 매장")
        _make_store(self.bob, name="밥 매장")
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_nonexistent_id_returns_not_found(self):
        _make_store(self.alice)
        response = self._delete(999_999, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_already_soft_deleted_returns_not_found(self):
        """idempotent 가 아니라 두 번째 호출은 404 (spec: '이미 삭제된 집기')."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        f.deleted_at = timezone.now()
        f.save(update_fields=["deleted_at"])
        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_staff_in_same_store_returns_not_found(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.STAFF)
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")
        # 삭제되지 않았어야 함
        f.refresh_from_db()
        self.assertIsNone(f.deleted_at)

    # ── admin role 통과 케이스 ─────────────────────────────────────────

    def test_creator_manager_can_delete(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertIsNotNone(f.deleted_at)

    def test_fellow_manager_can_delete(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.MANAGER)
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.bob_access)
        self.assertEqual(response.status_code, 200)
        f.refresh_from_db()
        self.assertIsNotNone(f.deleted_at)

    def test_fellow_vice_manager_can_delete(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.VICE_MANAGER)
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.bob_access)
        self.assertEqual(response.status_code, 200)

    def test_fellow_vmd_can_delete(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.VMD)
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.bob_access)
        self.assertEqual(response.status_code, 200)

    def test_multi_store_hub_admin_in_one_passes(self):
        """X 에서는 STAFF 지만 Y 에서는 MANAGER. creator(alice) 도 Y 멤버 → 통과."""
        store_x = _make_store(self.alice, name="X")
        store_y = Store.objects.create(
            user=self.alice,
            name="Y",
            address="부산",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=store_y, user=self.alice, role=StoreMember.Role.MANAGER
        )
        _add_member(store_x, self.bob, role=StoreMember.Role.STAFF)
        StoreMember.objects.create(
            store=store_y, user=self.bob, role=StoreMember.Role.MANAGER
        )
        f = _make_fixture(self.alice)
        response = self._delete(f.id, self.bob_access)
        self.assertEqual(response.status_code, 200)

    # ── soft-delete 동작 ─────────────────────────────────────────────

    def test_alive_versions_cascade_soft_deleted(self):
        """alice fixture 삭제 → alive version 들도 deleted_at 채워져야."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v1 = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        v2 = FixtureVersion.objects.create(fixture_master=f, version_name="v2")
        # 미리 dead 인 version 도 있다고 가정 — 그대로 유지되어야
        v_dead = FixtureVersion.objects.create(fixture_master=f, version_name="dead")
        original_dead_at = timezone.now() - timedelta(days=1)
        FixtureVersion.objects.filter(id=v_dead.id).update(deleted_at=original_dead_at)

        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)

        v1.refresh_from_db()
        v2.refresh_from_db()
        v_dead.refresh_from_db()
        self.assertIsNotNone(v1.deleted_at)
        self.assertIsNotNone(v2.deleted_at)
        # 이미 dead 였던 row 의 deleted_at 은 변하지 않아야 (alive() filter 로 제외됨)
        self.assertEqual(
            v_dead.deleted_at.replace(microsecond=0),
            original_dead_at.replace(microsecond=0),
        )

    def test_fixture_excluded_from_subsequent_list(self):
        """삭제 후 GET /fixtures 에서 안 보임."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        self._delete(f.id, self.alice_access)
        self.client.cookies["access_token"] = self.alice_access
        response = self.client.get("/api/v1/fixtures")
        ids = {item["fixture_id"] for item in response.json()["data"]["fixtures"]}
        self.assertNotIn(f.id, ids)

    # ── layout-in-use 차단 ────────────────────────────────────────────

    def test_layout_attached_returns_in_use(self):
        store = _make_store(self.alice)
        f = _make_fixture(self.alice)
        layout = Layout.objects.create(store=store, name="L1")
        _attach_fixture_to_layout(layout, f)

        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "FIXTURE_IN_USE")
        # 삭제 안 됐어야 함
        f.refresh_from_db()
        self.assertIsNone(f.deleted_at)

    def test_soft_deleted_layout_attachment_does_not_block(self):
        """soft-deleted layout 의 LayoutFixture 만 있으면 차단 대상 아님."""
        store = _make_store(self.alice)
        f = _make_fixture(self.alice)
        layout = Layout.objects.create(store=store, name="L1")
        _attach_fixture_to_layout(layout, f)
        layout.deleted_at = timezone.now()
        layout.save(update_fields=["deleted_at"])

        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)

    def test_soft_deleted_version_attachment_does_not_block(self):
        """soft-deleted FixtureVersion 의 LayoutFixture 만 있으면 차단 대상 아님."""
        store = _make_store(self.alice)
        f = _make_fixture(self.alice)
        layout = Layout.objects.create(store=store, name="L1")
        version = _attach_fixture_to_layout(layout, f)
        version.deleted_at = timezone.now()
        version.save(update_fields=["deleted_at"])

        response = self._delete(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        body = self._delete(f.id, self.alice_access).json()
        self.assertIn("success", body)
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "원형 집기가 성공적으로 삭제되었습니다.")
        self.assertIsNone(body["data"])


class FixtureVersionsListEndpointTests(TestCase):
    """GET /api/v1/fixtures/{fixture_id}/versions — 매장 단위 공유 (read 전용).

    가시성: list/detail 와 동일 (_visible_user_ids). 비가시/미존재/soft-deleted
    fixture 모두 FIXTURE_NOT_FOUND 404 (ID 프로빙 차단).
    권한: read 전용이라 admin-tier 게이트 없음 — STAFF 포함 매장 멤버 누구나.
    정렬: updated_at desc, id desc (tie-breaker). soft-deleted version 제외.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _get(self, fixture_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(f"/api/v1/fixtures/{fixture_id}/versions", **headers)

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._get(f.id)
        self.assertEqual(response.status_code, 401)

    # ── 가시성 — 모두 404 묶음 ─────────────────────────────────────────

    def test_no_store_membership_returns_not_found(self):
        f = _make_fixture(self.alice)  # alice 멤버십 0
        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_other_store_user_returns_not_found(self):
        _make_store(self.alice, name="앨리스 매장")
        _make_store(self.bob, name="밥 매장")
        f = _make_fixture(self.alice)
        FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._get(f.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_nonexistent_id_returns_not_found(self):
        _make_store(self.alice)
        response = self._get(999_999, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_soft_deleted_fixture_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        f.deleted_at = timezone.now()
        f.save(update_fields=["deleted_at"])
        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    # ── 권한 — STAFF 도 가능 (read 전용) ──────────────────────────────

    def test_staff_in_same_store_can_view(self):
        """admin-tier 게이트 없음 — STAFF 도 매장 멤버면 조회 가능."""
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.STAFF)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")

        response = self._get(f.id, self.bob_access)
        self.assertEqual(response.status_code, 200)
        ids = [item["version_id"] for item in response.json()["data"]["versions"]]
        self.assertEqual(ids, [v.id])

    def test_creator_sees_own_versions(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "집기의 진열 버전 목록 조회에 성공했습니다.")
        self.assertEqual(body["data"]["fixture_id"], f.id)
        items = body["data"]["versions"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["version_id"], v.id)
        self.assertEqual(items[0]["version_name"], "v1")
        self.assertIn("created_at", items[0])
        self.assertIn("updated_at", items[0])

    # ── 빈 케이스 ─────────────────────────────────────────────────────

    def test_no_versions_returns_empty_list(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._get(f.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["fixture_id"], f.id)
        self.assertEqual(body["versions"], [])

    # ── soft-deleted version 제외 ─────────────────────────────────────

    def test_soft_deleted_versions_excluded(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v_alive = FixtureVersion.objects.create(fixture_master=f, version_name="alive")
        v_dead = FixtureVersion.objects.create(fixture_master=f, version_name="dead")
        v_dead.deleted_at = timezone.now()
        v_dead.save(update_fields=["deleted_at"])

        response = self._get(f.id, self.alice_access)
        ids = {item["version_id"] for item in response.json()["data"]["versions"]}
        self.assertSetEqual(ids, {v_alive.id})
        self.assertNotIn(v_dead.id, ids)

    # ── 정렬 — updated_at desc, id desc ───────────────────────────────

    def test_ordered_by_updated_at_desc(self):
        """spec '최신순' — 최신 updated_at 먼저.

        v1 의 updated_at 을 명시적으로 미래 시각으로 강제 — `save(update_fields=...)` 는
        명시 안 한 auto_now 필드를 DB 에 반영하지 않으므로 in-memory 만 갱신되는 함정
        회피.
        """
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v1 = FixtureVersion.objects.create(fixture_master=f, version_name="oldest")
        v2 = FixtureVersion.objects.create(fixture_master=f, version_name="middle")
        v3 = FixtureVersion.objects.create(fixture_master=f, version_name="newest")

        later = timezone.now() + timedelta(seconds=60)
        FixtureVersion.objects.filter(id=v1.id).update(updated_at=later)

        response = self._get(f.id, self.alice_access)
        order = [item["version_id"] for item in response.json()["data"]["versions"]]
        self.assertEqual(order, [v1.id, v3.id, v2.id])

    def test_id_desc_tie_breaker(self):
        """동일 updated_at 일 때 id desc 로 정렬 — 안정 정렬 보장.

        same_time 을 자동 생성된 timestamp 보다 명백히 미래로 잡아 .update() 로
        세 row 모두 같은 시각으로 강제. (단순 now() 는 microsecond 단위로 v3 의
        auto-generated updated_at 보다 작을 수 있어 update 이후에도 v1/v2/v3 의
        값이 모두 동일하다고 보장 못 함.)
        """
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v1 = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        v2 = FixtureVersion.objects.create(fixture_master=f, version_name="v2")
        v3 = FixtureVersion.objects.create(fixture_master=f, version_name="v3")

        same_time = timezone.now() + timedelta(seconds=60)
        FixtureVersion.objects.filter(id__in=[v1.id, v2.id, v3.id]).update(
            updated_at=same_time
        )

        response = self._get(f.id, self.alice_access)
        order = [item["version_id"] for item in response.json()["data"]["versions"]]
        self.assertEqual(order, [v3.id, v2.id, v1.id])

    # ── 다른 fixture 의 version 격리 ──────────────────────────────────

    def test_other_fixtures_versions_not_included(self):
        """다른 fixture 의 version 이 섞이지 않아야."""
        _make_store(self.alice)
        f1 = _make_fixture(self.alice, name="f1")
        f2 = _make_fixture(self.alice, name="f2")
        v_in = FixtureVersion.objects.create(fixture_master=f1, version_name="in")
        v_out = FixtureVersion.objects.create(fixture_master=f2, version_name="out")

        response = self._get(f1.id, self.alice_access)
        ids = {item["version_id"] for item in response.json()["data"]["versions"]}
        self.assertSetEqual(ids, {v_in.id})
        self.assertNotIn(v_out.id, ids)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        body = self._get(f.id, self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        data_keys = set(body["data"].keys())
        self.assertSetEqual(data_keys, {"fixture_id", "versions"})
        item_keys = set(body["data"]["versions"][0].keys())
        self.assertSetEqual(
            item_keys,
            {"version_id", "version_name", "created_at", "updated_at"},
        )


class FixtureVersionCreateEndpointTests(TestCase):
    """POST /api/v1/fixtures/{fixture_id}/versions — 매장 단위 공유 정책.

    권한: 매장 멤버 누구나 (STAFF 포함, 일상 진열 작업 — admin tier 게이트 없음).
    가시성: 본인 + 본인이 속한 매장의 다른 멤버가 등록한 fixture (alive only).
    비가시/미존재/soft-deleted 모두 FIXTURE_NOT_FOUND 404 로 묶음.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _url(self, fixture_id):
        return f"/api/v1/fixtures/{fixture_id}/versions"

    def _post(self, fixture_id, body, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.post(
            self._url(fixture_id),
            data=body,
            content_type="application/json",
            **headers,
        )

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": "v1"})
        self.assertEqual(response.status_code, 401)

    # ── 가시성 게이트 ─────────────────────────────────────────────────

    def test_no_store_membership_returns_not_found(self):
        """매장 멤버십 없는 사용자는 자기 fixture 도 못 봄 (정책상)."""
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": "v1"}, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_other_store_user_returns_not_found(self):
        """alice/bob 다른 매장 → bob 은 alice 의 fixture 비가시."""
        _make_store(self.alice, name="A 매장")
        _make_store(self.bob, name="B 매장")
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": "v1"}, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_nonexistent_fixture_returns_not_found(self):
        _make_store(self.alice)
        response = self._post(99999, {"version_name": "v1"}, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    def test_soft_deleted_fixture_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        f.soft_delete()
        response = self._post(f.id, {"version_name": "v1"}, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "FIXTURE_NOT_FOUND")

    # ── 성공 — 매장 멤버 누구나 (STAFF 포함) ──────────────────────────

    def test_creator_can_create_version(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._post(
            f.id, {"version_name": "2026 F/W 가을 신상 배치안"}, self.alice_access
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["fixture_id"], f.id)
        self.assertEqual(data["version_name"], "2026 F/W 가을 신상 배치안")
        self.assertIn("version_id", data)
        self.assertIn("created_at", data)

        version = FixtureVersion.objects.get(id=data["version_id"])
        self.assertEqual(version.fixture_master_id, f.id)
        self.assertEqual(version.version_name, "2026 F/W 가을 신상 배치안")
        self.assertIsNone(version.deleted_at)

    def test_fellow_staff_can_create_version(self):
        """STAFF 도 일상 진열 작업이라 시안 추가 가능."""
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.STAFF)
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": "v-by-staff"}, self.bob_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["version_name"], "v-by-staff")

    def test_version_name_stripped_on_save(self):
        """양 끝 공백은 의미 없으므로 strip 후 저장."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": "  v1  "}, self.alice_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["version_name"], "v1")

    # ── INVALID_PARAMETER ────────────────────────────────────────────

    def test_empty_version_name_returns_invalid_parameter(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": ""}, self.alice_access)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")
        self.assertFalse(FixtureVersion.objects.filter(fixture_master=f).exists())

    def test_whitespace_only_version_name_returns_invalid_parameter(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._post(f.id, {"version_name": "   "}, self.alice_access)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")
        self.assertFalse(FixtureVersion.objects.filter(fixture_master=f).exists())

    def test_missing_version_name_returns_validation_error(self):
        """body 에 version_name 키 자체가 없으면 Pydantic 422."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._post(f.id, {}, self.alice_access)
        self.assertEqual(response.status_code, 422)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        body = self._post(f.id, {"version_name": "v1"}, self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        self.assertSetEqual(
            set(body["data"].keys()),
            {"version_id", "fixture_id", "version_name", "created_at"},
        )


def _make_placement(version, *, sku_code=None, x=0, y=0, z=0, memo=None):
    """version 위에 placement 1건 시드. variant 도 함께 새로 생성."""
    pm = ProductMaster.objects.create(
        user=version.fixture_master.user, width=10, height=10
    )
    variant = ProductVariant.objects.create(product_master=pm, sku_code=sku_code)
    return FixtureVersionProduct.objects.create(
        fixture_version=version,
        variant=variant,
        local_pos_x=x,
        local_pos_y=y,
        local_pos_z=z,
        memo=memo,
    )


class FixtureVersionPlacementsListEndpointTests(TestCase):
    """GET /api/v1/fixtures/{fixture_id}/versions/{version_id}/placements.

    권한: 매장 멤버 누구나 (read 전용, STAFF 포함). 가시성: get_visible_version
    가 단일화 — 비가시 fixture/version, 정합 위반, soft-deleted 모두
    VERSION_NOT_FOUND 404 로 묶음 (ID 프로빙 차단).
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _url(self, fixture_id, version_id):
        return f"/api/v1/fixtures/{fixture_id}/versions/{version_id}/placements"

    def _get(self, fixture_id, version_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self._url(fixture_id, version_id), **headers)

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._get(f.id, v.id)
        self.assertEqual(response.status_code, 401)

    # ── 가시성 게이트 (모두 VERSION_NOT_FOUND 404 통합) ────────────────

    def test_no_store_membership_returns_not_found(self):
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._get(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_other_store_user_returns_not_found(self):
        _make_store(self.alice, name="A 매장")
        _make_store(self.bob, name="B 매장")
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._get(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_nonexistent_fixture_returns_not_found(self):
        _make_store(self.alice)
        response = self._get(99999, 1, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_nonexistent_version_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._get(f.id, 99999, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_soft_deleted_fixture_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        f.soft_delete()
        response = self._get(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_soft_deleted_version_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        v.soft_delete()
        response = self._get(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_version_belongs_to_other_fixture_returns_not_found(self):
        """version 은 alive 지만 다른 fixture 소속 → 정합 위반 시 404."""
        _make_store(self.alice)
        f1 = _make_fixture(self.alice, name="f1")
        f2 = _make_fixture(self.alice, name="f2")
        v_in_f2 = FixtureVersion.objects.create(fixture_master=f2, version_name="v")
        response = self._get(f1.id, v_in_f2.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    # ── 성공 — 매장 멤버 누구나 ───────────────────────────────────────

    def test_creator_sees_empty_placements(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._get(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["version_id"], v.id)
        self.assertEqual(data["placements"], [])

    def test_creator_sees_populated_placements(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        p1 = _make_placement(v, sku_code="HD-GRY-L", x=-30, y=120, z=10, memo="앞줄")
        p2 = _make_placement(v, sku_code="HD-GRY-XL", x=40, y=120, z=10)

        response = self._get(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["placements"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["placement_id"], p1.id)
        self.assertEqual(items[0]["local_pos_x"], -30)
        self.assertEqual(items[0]["local_pos_y"], 120)
        self.assertEqual(items[0]["local_pos_z"], 10)
        self.assertEqual(items[0]["status"], "DISPLAY")
        self.assertEqual(items[0]["memo"], "앞줄")
        self.assertEqual(items[0]["variant"]["variant_id"], p1.variant_id)
        self.assertEqual(items[0]["variant"]["sku_code"], "HD-GRY-L")
        self.assertEqual(items[1]["placement_id"], p2.id)
        self.assertIsNone(items[1]["memo"])

    def test_fellow_staff_sees_placements(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.STAFF)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        _make_placement(v, sku_code="X")
        response = self._get(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]["placements"]), 1)

    def test_variant_with_null_sku_code(self):
        """sku_code 는 nullable — 응답에 None 그대로 노출."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        _make_placement(v, sku_code=None)
        response = self._get(f.id, v.id, self.alice_access)
        item = response.json()["data"]["placements"][0]
        self.assertIsNone(item["variant"]["sku_code"])

    def test_ordered_by_placement_id_asc(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        p1 = _make_placement(v, sku_code="A")
        p2 = _make_placement(v, sku_code="B")
        p3 = _make_placement(v, sku_code="C")
        response = self._get(f.id, v.id, self.alice_access)
        order = [item["placement_id"] for item in response.json()["data"]["placements"]]
        self.assertEqual(order, [p1.id, p2.id, p3.id])

    def test_other_versions_placements_not_included(self):
        """같은 fixture 의 다른 version 의 placement 가 섞이지 않아야."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v_target = FixtureVersion.objects.create(
            fixture_master=f, version_name="target"
        )
        v_other = FixtureVersion.objects.create(fixture_master=f, version_name="other")
        p_in = _make_placement(v_target, sku_code="IN")
        _make_placement(v_other, sku_code="OUT")

        response = self._get(f.id, v_target.id, self.alice_access)
        ids = {item["placement_id"] for item in response.json()["data"]["placements"]}
        self.assertSetEqual(ids, {p_in.id})

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        _make_placement(v, sku_code="X", memo="m")
        body = self._get(f.id, v.id, self.alice_access).json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        self.assertSetEqual(set(body["data"].keys()), {"version_id", "placements"})
        item_keys = set(body["data"]["placements"][0].keys())
        self.assertSetEqual(
            item_keys,
            {
                "placement_id",
                "local_pos_x",
                "local_pos_y",
                "local_pos_z",
                "status",
                "memo",
                "variant",
            },
        )
        variant_keys = set(body["data"]["placements"][0]["variant"].keys())
        self.assertSetEqual(variant_keys, {"variant_id", "sku_code"})


class FixtureVersionDeleteEndpointTests(TestCase):
    """DELETE /api/v1/fixtures/{fixture_id}/versions/{version_id} — admin-tier 만,
    layout 사용 중이면 차단.

    가시성+admin 실패는 모두 VERSION_NOT_FOUND 404 로 묶음 (spec line 20, ID
    프로빙 차단). soft-delete: fixture_versions.deleted_at 만 갱신. 이미 삭제된
    버전 두 번째 호출도 .alive() filter 로 자연 404.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))

    def _delete(self, fixture_id, version_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.delete(
            f"/api/v1/fixtures/{fixture_id}/versions/{version_id}", **headers
        )

    # ── 인증 ───────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id)
        self.assertEqual(response.status_code, 401)

    # ── 가시성 / admin-tier — 모두 VERSION_NOT_FOUND 404 묶음 ──────────

    def test_no_store_membership_returns_not_found(self):
        f = _make_fixture(self.alice)  # alice 멤버십 0
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_other_store_user_returns_not_found(self):
        _make_store(self.alice, name="앨리스 매장")
        _make_store(self.bob, name="밥 매장")
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_nonexistent_version_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        response = self._delete(f.id, 999_999, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_nonexistent_fixture_returns_not_found(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(999_999, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_version_belongs_to_other_fixture_returns_not_found(self):
        """version_id ↔ fixture_id 정합 위반 — version 은 alive 지만 다른 fixture 소속."""
        _make_store(self.alice)
        f1 = _make_fixture(self.alice, name="f1")
        f2 = _make_fixture(self.alice, name="f2")
        v_in_f2 = FixtureVersion.objects.create(fixture_master=f2, version_name="v1")
        response = self._delete(f1.id, v_in_f2.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")
        # f2.v 는 살아있어야 함
        v_in_f2.refresh_from_db()
        self.assertIsNone(v_in_f2.deleted_at)

    def test_already_soft_deleted_version_returns_not_found(self):
        """idempotent 가 아니라 두 번째 호출은 404."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        v.deleted_at = timezone.now()
        v.save(update_fields=["deleted_at"])
        response = self._delete(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")

    def test_soft_deleted_fixture_returns_not_found(self):
        """parent fixture 가 soft-deleted 면 version 은 alive 라도 차단."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        f.deleted_at = timezone.now()
        f.save(update_fields=["deleted_at"])
        response = self._delete(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")
        v.refresh_from_db()
        self.assertIsNone(v.deleted_at)

    def test_staff_in_same_store_returns_not_found(self):
        """STAFF 는 가시 범위지만 admin 아님 → 404 (spec: STAFF 도 NOT_FOUND 묶음)."""
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.STAFF)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "VERSION_NOT_FOUND")
        # 삭제 안 됐어야 함
        v.refresh_from_db()
        self.assertIsNone(v.deleted_at)

    # ── admin role 통과 케이스 ─────────────────────────────────────────

    def test_creator_manager_can_delete(self):
        _make_store(self.alice)  # alice 가 MANAGER
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        v.refresh_from_db()
        self.assertIsNotNone(v.deleted_at)

    def test_fellow_manager_can_delete(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.MANAGER)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 200)
        v.refresh_from_db()
        self.assertIsNotNone(v.deleted_at)

    def test_fellow_vice_manager_can_delete(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.VICE_MANAGER)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 200)

    def test_fellow_vmd_can_delete(self):
        store = _make_store(self.alice)
        _add_member(store, self.bob, role=StoreMember.Role.VMD)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 200)

    def test_multi_store_hub_admin_in_one_passes(self):
        """X 에서는 STAFF 지만 Y 에서는 MANAGER. creator(alice) 도 Y 멤버 → 통과."""
        store_x = _make_store(self.alice, name="X")
        store_y = Store.objects.create(
            user=self.alice,
            name="Y",
            address="부산",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=store_y, user=self.alice, role=StoreMember.Role.MANAGER
        )
        _add_member(store_x, self.bob, role=StoreMember.Role.STAFF)
        StoreMember.objects.create(
            store=store_y, user=self.bob, role=StoreMember.Role.MANAGER
        )
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        response = self._delete(f.id, v.id, self.bob_access)
        self.assertEqual(response.status_code, 200)

    # ── soft-delete 동작 ─────────────────────────────────────────────

    def test_only_target_version_soft_deleted(self):
        """삭제는 지정 version 만 — 같은 fixture 의 다른 alive version 은 그대로."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v1 = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        v2 = FixtureVersion.objects.create(fixture_master=f, version_name="v2")

        response = self._delete(f.id, v1.id, self.alice_access)
        self.assertEqual(response.status_code, 200)

        v1.refresh_from_db()
        v2.refresh_from_db()
        self.assertIsNotNone(v1.deleted_at)
        self.assertIsNone(v2.deleted_at)

    def test_fixture_master_not_affected(self):
        """version 삭제는 parent fixture 에 영향 없음."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        self._delete(f.id, v.id, self.alice_access)
        f.refresh_from_db()
        self.assertIsNone(f.deleted_at)

    def test_version_excluded_from_subsequent_list(self):
        """삭제 후 GET /versions 에서 안 보임."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        self._delete(f.id, v.id, self.alice_access)
        self.client.cookies["access_token"] = self.alice_access
        response = self.client.get(f"/api/v1/fixtures/{f.id}/versions")
        ids = {item["version_id"] for item in response.json()["data"]["versions"]}
        self.assertNotIn(v.id, ids)

    def test_placements_rows_preserved(self):
        """FixtureVersionProduct 는 SoftDeleteModel 아니라 row 자체는 유지 —
        부모 version 이 soft-deleted 라 read-side 에서 자연 무시 (delete_fixture 정합)."""
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        placement = _seed_alive_placement(f)
        version = placement.fixture_version
        self._delete(f.id, version.id, self.alice_access)
        self.assertTrue(FixtureVersionProduct.objects.filter(id=placement.id).exists())

    # ── layout-in-use 차단 ────────────────────────────────────────────

    def test_layout_attached_returns_in_use(self):
        store = _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        layout = Layout.objects.create(store=store, name="L1")
        _lf_create(
            layout=layout,
            fixture_version=v,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )

        response = self._delete(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "VERSION_IN_USE")
        v.refresh_from_db()
        self.assertIsNone(v.deleted_at)

    def test_soft_deleted_layout_attachment_does_not_block(self):
        """soft-deleted layout 의 LayoutFixture 만 있으면 차단 대상 아님
        (delete_fixture 의 cascade 정책 정합)."""
        store = _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        layout = Layout.objects.create(store=store, name="L1")
        _lf_create(
            layout=layout,
            fixture_version=v,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )
        layout.deleted_at = timezone.now()
        layout.save(update_fields=["deleted_at"])

        response = self._delete(f.id, v.id, self.alice_access)
        self.assertEqual(response.status_code, 200)
        v.refresh_from_db()
        self.assertIsNotNone(v.deleted_at)

    def test_other_version_attached_does_not_block(self):
        """같은 fixture 의 다른 version 이 layout 에 attach 됐어도 — 대상 version 만
        검사하므로 차단 대상 아님."""
        store = _make_store(self.alice)
        f = _make_fixture(self.alice)
        v_target = FixtureVersion.objects.create(
            fixture_master=f, version_name="target"
        )
        v_other = FixtureVersion.objects.create(fixture_master=f, version_name="other")
        layout = Layout.objects.create(store=store, name="L1")
        _lf_create(
            layout=layout,
            fixture_version=v_other,
            world_pos_x=0,
            world_pos_y=0,
            world_pos_z=0,
            world_rot_y=0,
        )

        response = self._delete(f.id, v_target.id, self.alice_access)
        self.assertEqual(response.status_code, 200)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        _make_store(self.alice)
        f = _make_fixture(self.alice)
        v = FixtureVersion.objects.create(fixture_master=f, version_name="v1")
        body = self._delete(f.id, v.id, self.alice_access).json()
        self.assertIn("success", body)
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "집기 진열 버전이 성공적으로 삭제되었습니다.")
        self.assertIsNone(body["data"])


def _make_visible_variant(store, *, user=None, sku_code=None):
    """매장 가시성(store_products bridge) 까지 셋업된 variant 1건 시드.

    PATCH placements 의 variant 가시성 검증은 products._visible_master_queryset
    (store_products bridge 기반) 을 사용하므로, _make_placement 의 단순 variant
    만으론 검증 통과 못 함 — bridge 명시적 생성 필요.
    """
    pm = ProductMaster.objects.create(user=user or store.user, width=10, height=10)
    StoreProduct.objects.create(store=store, product_master=pm)
    return ProductVariant.objects.create(product_master=pm, sku_code=sku_code)


class FixtureVersionPlacementsUpdateEndpointTests(TestCase):
    """PATCH /api/v1/fixtures/{fixture_id}/versions/{version_id}/placements
    — 집기 내부 상품 배치 벌크 동기화 (UPSERT + 누락분 hard delete).

    권한: 매장 멤버 누구나 (STAFF 포함, spec line 20).
    가시성 실패: 모두 VERSION_NOT_FOUND 404 로 묶음 (ID 프로빙 차단).
    검증 실패: INVALID_PLACEMENT_ID / INVALID_VARIANT_ID 422, DB 변화 0 (all-or-nothing).
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            email="alice@example.com",
            password="Pwd1234!",
            name="앨리스",
            confirmed=True,
        )
        self.bob = User.objects.create_user(
            email="bob@example.com",
            password="Pwd1234!",
            name="밥",
            confirmed=True,
        )
        self.carol = User.objects.create_user(
            email="carol@example.com",
            password="Pwd1234!",
            name="캐롤",
            confirmed=True,
        )
        self.alice_access = str(AccessToken.for_user(self.alice))
        self.bob_access = str(AccessToken.for_user(self.bob))
        self.carol_access = str(AccessToken.for_user(self.carol))

        self.store_a = _make_store(self.alice, name="A 매장")
        _add_member(self.store_a, self.bob, role=StoreMember.Role.STAFF)
        self.store_b = _make_store(self.carol, name="B 매장")

        self.fixture = _make_fixture(self.alice)
        self.version = FixtureVersion.objects.create(
            fixture_master=self.fixture, version_name="v1"
        )

        # 매장 A 가시 variants (store_products bridge 등록됨)
        self.variant_a1 = _make_visible_variant(self.store_a, sku_code="A1")
        self.variant_a2 = _make_visible_variant(self.store_a, sku_code="A2")
        self.variant_a3 = _make_visible_variant(self.store_a, sku_code="A3")
        # 매장 B 의 variant — alice/bob 의 가시 범위 밖
        self.variant_b = _make_visible_variant(self.store_b, sku_code="B1")

    def _url(self, fixture_id, version_id):
        return f"/api/v1/fixtures/{fixture_id}/versions/{version_id}/placements"

    def _patch(self, fixture_id, version_id, body, access_token=None):
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.patch(
            self._url(fixture_id, version_id),
            data=body,
            content_type="application/json",
        )

    # ── 정상 시나리오 ─────────────────────────────────────────────────

    def test_pure_insert(self):
        """빈 version 에 신규 placement 3건 INSERT."""
        body = {
            "placements": [
                {
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 10,
                    "local_pos_y": 0,
                    "local_pos_z": 30,
                },
                {
                    "variant_id": self.variant_a2.id,
                    "local_pos_x": 20,
                    "local_pos_y": 0,
                    "local_pos_z": 30,
                },
                {
                    "variant_id": self.variant_a3.id,
                    "local_pos_x": 30,
                    "local_pos_y": 0,
                    "local_pos_z": 30,
                },
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["version_id"], self.version.id)
        self.assertEqual(data["updated_count"], 3)
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(
            FixtureVersionProduct.objects.filter(fixture_version=self.version).count(),
            3,
        )

    def test_pure_update(self):
        """기존 placement 의 좌표·메모 변경 — id 유지."""
        p = FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=self.variant_a1,
            local_pos_x=0,
            local_pos_y=0,
            local_pos_z=0,
            memo="before",
        )
        body = {
            "placements": [
                {
                    "placement_id": p.id,
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 99,
                    "local_pos_y": 88,
                    "local_pos_z": 77,
                    "memo": "after",
                }
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["updated_count"], 1)
        self.assertEqual(data["deleted_count"], 0)

        p.refresh_from_db()
        self.assertEqual(p.local_pos_x, 99)
        self.assertEqual(p.local_pos_y, 88)
        self.assertEqual(p.local_pos_z, 77)
        self.assertEqual(p.memo, "after")

    def test_mixed_upsert_delete(self):
        """UPDATE + INSERT + 누락분 DELETE 혼합 — spec 의 핵심 시나리오."""
        p_keep = FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=self.variant_a1,
            local_pos_x=0,
            local_pos_y=0,
            local_pos_z=0,
        )
        p_drop = FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=self.variant_a2,
            local_pos_x=0,
            local_pos_y=0,
            local_pos_z=0,
        )

        body = {
            "placements": [
                {
                    "placement_id": p_keep.id,
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 50,
                    "local_pos_y": 0,
                    "local_pos_z": 0,
                },  # UPDATE
                {
                    "variant_id": self.variant_a3.id,
                    "local_pos_x": 60,
                    "local_pos_y": 0,
                    "local_pos_z": 0,
                },  # INSERT
                # p_drop 누락 → DELETE
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["updated_count"], 2)
        self.assertEqual(data["deleted_count"], 1)

        self.assertTrue(FixtureVersionProduct.objects.filter(id=p_keep.id).exists())
        self.assertFalse(FixtureVersionProduct.objects.filter(id=p_drop.id).exists())
        self.assertEqual(
            FixtureVersionProduct.objects.filter(fixture_version=self.version).count(),
            2,
        )

    def test_empty_array_deletes_all(self):
        """`placements: []` → version 의 모든 placement hard delete."""
        FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=self.variant_a1,
            local_pos_x=0,
            local_pos_y=0,
            local_pos_z=0,
        )
        FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=self.variant_a2,
            local_pos_x=0,
            local_pos_y=0,
            local_pos_z=0,
        )

        resp = self._patch(
            self.fixture.id, self.version.id, {"placements": []}, self.alice_access
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["updated_count"], 0)
        self.assertEqual(data["deleted_count"], 2)
        self.assertFalse(
            FixtureVersionProduct.objects.filter(fixture_version=self.version).exists()
        )

    # ── 권한·존재 에러 ─────────────────────────────────────────────────

    def test_version_not_found_unknown_id(self):
        resp = self._patch(
            self.fixture.id, 99999, {"placements": []}, self.alice_access
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "VERSION_NOT_FOUND")

    def test_version_not_found_other_store(self):
        """carol 이 alice 의 version 에 접근 — 가시 밖, 404."""
        resp = self._patch(
            self.fixture.id, self.version.id, {"placements": []}, self.carol_access
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "VERSION_NOT_FOUND")

    def test_unauthenticated(self):
        resp = self._patch(self.fixture.id, self.version.id, {"placements": []})
        self.assertEqual(resp.status_code, 401)

    # ── 검증 에러 (422, DB 변화 0) ────────────────────────────────────

    def test_invalid_placement_id_cross_version(self):
        """placement_id 가 다른 version 소속 — INVALID_PLACEMENT_ID 422."""
        other_version = FixtureVersion.objects.create(
            fixture_master=self.fixture, version_name="other"
        )
        p_in_other = FixtureVersionProduct.objects.create(
            fixture_version=other_version,
            variant=self.variant_a1,
            local_pos_x=0,
            local_pos_y=0,
            local_pos_z=0,
        )
        body = {
            "placements": [
                {
                    "placement_id": p_in_other.id,
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 1,
                    "local_pos_y": 1,
                    "local_pos_z": 1,
                }
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_PLACEMENT_ID")
        # other_version 의 placement 가 영향받지 않았는지
        p_in_other.refresh_from_db()
        self.assertEqual(p_in_other.local_pos_x, 0)

    def test_invalid_variant_id_not_visible(self):
        """variant_id 가 carol 의 매장 (가시 밖) — INVALID_VARIANT_ID 422."""
        body = {
            "placements": [
                {
                    "variant_id": self.variant_b.id,
                    "local_pos_x": 1,
                    "local_pos_y": 1,
                    "local_pos_z": 1,
                }
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_VARIANT_ID")
        self.assertFalse(
            FixtureVersionProduct.objects.filter(fixture_version=self.version).exists()
        )

    def test_invalid_variant_id_soft_deleted(self):
        """variant 가 soft-deleted — INVALID_VARIANT_ID 422."""
        self.variant_a1.deleted_at = timezone.now()
        self.variant_a1.save(update_fields=["deleted_at"])
        body = {
            "placements": [
                {
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 1,
                    "local_pos_y": 1,
                    "local_pos_z": 1,
                }
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_VARIANT_ID")

    def test_all_or_nothing(self):
        """3개 row 중 마지막이 invalid — DB 변화 0 (좌표·count 모두)."""
        p_keep = FixtureVersionProduct.objects.create(
            fixture_version=self.version,
            variant=self.variant_a1,
            local_pos_x=10,
            local_pos_y=10,
            local_pos_z=10,
        )
        body = {
            "placements": [
                {
                    "placement_id": p_keep.id,
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 99,
                    "local_pos_y": 99,
                    "local_pos_z": 99,
                },
                {
                    "variant_id": self.variant_a2.id,
                    "local_pos_x": 1,
                    "local_pos_y": 1,
                    "local_pos_z": 1,
                },
                {
                    "variant_id": self.variant_b.id,  # invalid — 가시 밖
                    "local_pos_x": 2,
                    "local_pos_y": 2,
                    "local_pos_z": 2,
                },
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.alice_access)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "INVALID_VARIANT_ID")

        # DB 변화 0 검증
        p_keep.refresh_from_db()
        self.assertEqual(p_keep.local_pos_x, 10)
        self.assertEqual(p_keep.local_pos_y, 10)
        self.assertEqual(p_keep.local_pos_z, 10)
        self.assertEqual(
            FixtureVersionProduct.objects.filter(fixture_version=self.version).count(),
            1,
        )

    # ── 권한 — STAFF 통과 ─────────────────────────────────────────────

    def test_staff_can_update(self):
        """STAFF (bob) 도 정상 동작 — admin tier 게이트 안 끼었는지 확인."""
        body = {
            "placements": [
                {
                    "variant_id": self.variant_a1.id,
                    "local_pos_x": 5,
                    "local_pos_y": 5,
                    "local_pos_z": 5,
                }
            ]
        }
        resp = self._patch(self.fixture.id, self.version.id, body, self.bob_access)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["updated_count"], 1)
