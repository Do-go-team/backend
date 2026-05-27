import json
import shutil
import tempfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.utils import timezone
from ninja_jwt.tokens import AccessToken
from PIL import Image

from assets_3d.models import Asset3D
from products.models import ProductMaster, ProductVariant, StoreInventory, StoreProduct
from stores.models import Store, StoreMember
from users.models import User


# 165 multipart endpoint 테스트에서 ImageField 가 실제 디스크 쓰기 — MEDIA_ROOT 격리.
_MEDIA_TMP = tempfile.mkdtemp(prefix="dogo_test_media_")


def _make_image(name="bar.jpg"):
    """ImageField 검증 통과용 작은 JPEG (PIL 의존). 호출자가 SimpleUploadedFile 받음."""
    buf = BytesIO()
    Image.new("RGB", (10, 10), color="black").save(buf, format="JPEG")
    buf.seek(0)
    return SimpleUploadedFile(name, buf.read(), content_type="image/jpeg")


class ProductCreateEndpointTests(TestCase):
    """POST /api/v1/products — 사진 촬영 시나리오의 master+variant 일괄 등록
    + 매장 단위 가시성 bridge (store_products) 채움.

    책임:
      - 입력: store_id + AI 가 주는 image_url/width/height array
      - master + variant + store_products row 한 transaction.atomic 에 일괄 INSERT
      - 사용자가 store_id 의 멤버 아니면 STORE_NOT_FOUND 404

    null 정책: 163 그대로 (master.name/price/depth + variant.size/color/sku_code/barcode null).

    sku_code unique 는 NULL 다중 허용 (PG) 으로 충돌 없이 placeholder 다중 생성 가능.
    """

    url = "/api/v1/products"

    def setUp(self):
        self.user = User.objects.create_user(
            email="user@example.com",
            password="User1234!",
            name="사용자",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

        # 사용자가 OWNER 인 매장 — 모든 성공 시나리오에서 store_id 로 사용
        self.store = Store.objects.create(
            user=self.user,
            name="DO-GO 성수",
            address="서울 성동구",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )
        StoreMember.objects.create(
            store=self.store,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )

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

    def _payload(self, products=None, store_id=None):
        if products is None:
            products = [
                {
                    "image_url": "https://example.com/crop_1.jpg",
                    "width": 30,
                    "height": 12,
                },
                {
                    "image_url": "https://example.com/crop_2.jpg",
                    "width": 25,
                    "height": 15,
                },
            ]
        return {
            "store_id": store_id if store_id is not None else self.store.id,
            "products": products,
        }

    # ── 성공 ───────────────────────────────────────────────────────────

    def test_success_single_product(self):
        payload = self._payload(
            products=[
                {"image_url": "https://s3.../shoe.jpg", "width": 30, "height": 12},
            ]
        )
        response = self._post(payload, self.access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "상품이 성공적으로 등록되었습니다.")

        items = body["data"]["products"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("master_id", item)
        self.assertIn("variant_id", item)
        self.assertEqual(item["image_url"], "https://s3.../shoe.jpg")
        self.assertEqual(item["width"], 30)
        self.assertEqual(item["height"], 12)
        self.assertIn("created_at", item)

        # 응답 키 잠금 — refactor 시 필드 누설 방지
        self.assertEqual(
            set(item.keys()),
            {"master_id", "variant_id", "image_url", "width", "height", "created_at"},
        )

        # DB 반영 — master + variant 쌍 정확히 1건
        master = ProductMaster.objects.get(id=item["master_id"])
        self.assertEqual(master.user_id, self.user.id)
        self.assertEqual(master.width, 30)
        self.assertEqual(master.height, 12)
        self.assertIsNone(master.name)
        self.assertIsNone(master.price)
        self.assertIsNone(master.depth)

        variant = ProductVariant.objects.get(id=item["variant_id"])
        self.assertEqual(variant.product_master_id, master.id)
        self.assertIsNone(variant.size)
        self.assertIsNone(variant.color)
        self.assertIsNone(variant.sku_code)

        # store_products bridge row 도 같이 생성됨 (매장 가시성)
        self.assertTrue(
            StoreProduct.objects.filter(
                store=self.store,
                product_master=master,
            ).exists()
        )

    def test_success_multiple_products(self):
        """AI 가 사진에서 객체 3개 인식 → master+variant+store_products 3쌍 생성."""
        payload = self._payload(
            products=[
                {"image_url": "https://s3.../a.jpg", "width": 10, "height": 10},
                {"image_url": "https://s3.../b.jpg", "width": 20, "height": 20},
                {"image_url": "https://s3.../c.jpg", "width": 30, "height": 30},
            ]
        )
        response = self._post(payload, self.access)

        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["products"]
        self.assertEqual(len(items), 3)

        # 입력 순서 보존
        self.assertEqual([it["width"] for it in items], [10, 20, 30])
        # master id 와 variant id 각각 unique
        self.assertEqual(len({it["master_id"] for it in items}), 3)
        self.assertEqual(len({it["variant_id"] for it in items}), 3)

        # DB 반영
        self.assertEqual(ProductMaster.objects.filter(user=self.user).count(), 3)
        self.assertEqual(ProductVariant.objects.count(), 3)
        self.assertEqual(StoreProduct.objects.filter(store=self.store).count(), 3)

    def test_success_null_sku_code_allows_duplicates(self):
        """sku_code unique 제약은 NULL 다중 허용 (PostgreSQL 동작).
        같은 사용자가 사진을 여러 번 찍어도 placeholder variant 들 충돌 없음."""
        self._post(
            self._payload(
                products=[
                    {"image_url": "https://s3.../a.jpg", "width": 10, "height": 10},
                ]
            ),
            self.access,
        )
        response = self._post(
            self._payload(
                products=[
                    {"image_url": "https://s3.../b.jpg", "width": 20, "height": 20},
                ]
            ),
            self.access,
        )

        self.assertEqual(response.status_code, 200)
        # variant 2개 모두 sku_code IS NULL — 충돌 없음
        self.assertEqual(
            ProductVariant.objects.filter(sku_code__isnull=True).count(), 2
        )

    # ── 인증 ──────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        response = self._post(self._payload())
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED_USER")

    def test_invalid_token_returns_401(self):
        response = self._post(self._payload(), "not.a.valid.token")
        self.assertEqual(response.status_code, 401)

    # ── 입력 검증 ─────────────────────────────────────────────────────

    def test_empty_products_array_returns_422(self):
        """빈 배열은 의미 없는 호출 — min_length=1 으로 schema 단계에서 거부."""
        response = self._post({"products": []}, self.access)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_PARAMETER")

    def test_missing_products_key_returns_422(self):
        response = self._post({}, self.access)
        self.assertEqual(response.status_code, 422)

    def test_missing_required_field_in_item_returns_422(self):
        for field in ("image_url", "width", "height"):
            with self.subTest(field=field):
                item = {"image_url": "https://s3.../a.jpg", "width": 10, "height": 10}
                del item[field]
                response = self._post({"products": [item]}, self.access)
                self.assertEqual(response.status_code, 422)

    def test_zero_or_negative_dimensions_returns_422(self):
        """width/height ≥ 1 — 0 이하는 비현실적."""
        for field, bad_value in (
            ("width", 0),
            ("width", -1),
            ("height", 0),
            ("height", -5),
        ):
            with self.subTest(field=field, bad_value=bad_value):
                item = {"image_url": "https://s3.../a.jpg", "width": 10, "height": 10}
                item[field] = bad_value
                response = self._post({"products": [item]}, self.access)
                self.assertEqual(response.status_code, 422)

    def test_empty_image_url_returns_422(self):
        item = {"image_url": "", "width": 10, "height": 10}
        response = self._post({"products": [item]}, self.access)
        self.assertEqual(response.status_code, 422)

    def test_image_url_too_long_returns_422(self):
        item = {"image_url": "https://" + "x" * 510, "width": 10, "height": 10}
        response = self._post({"products": [item]}, self.access)
        self.assertEqual(response.status_code, 422)

    def test_partial_failure_rolls_back_all(self):
        """배열 안 한 row 라도 invalid 면 *어떤 master 도 생성되지 않음* (atomic).
        store_products row 도 마찬가지."""
        payload = {
            "store_id": self.store.id,
            "products": [
                {"image_url": "https://s3.../a.jpg", "width": 10, "height": 10},
                {
                    "image_url": "https://s3.../b.jpg",
                    "width": -1,
                    "height": 10,
                },  # invalid
            ],
        }
        response = self._post(payload, self.access)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(ProductMaster.objects.count(), 0)
        self.assertEqual(ProductVariant.objects.count(), 0)
        self.assertEqual(StoreProduct.objects.count(), 0)

    # ── 매장 권한 검증 ────────────────────────────────────────────────

    def test_missing_store_id_returns_422(self):
        """store_id 누락 — schema validation 실패."""
        payload = {
            "products": [
                {"image_url": "https://s3.../a.jpg", "width": 10, "height": 10},
            ]
        }
        response = self._post(payload, self.access)
        self.assertEqual(response.status_code, 422)

    def test_nonexistent_store_returns_404(self):
        """없는 매장 ID → STORE_NOT_FOUND. DB 에 master 도 안 만들어짐."""
        response = self._post(self._payload(store_id=99999), self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")
        self.assertEqual(ProductMaster.objects.count(), 0)

    def test_non_member_store_returns_404(self):
        """사용자가 멤버 아닌 매장 → STORE_NOT_FOUND (ID 프로빙 차단)."""
        outsider_store = Store.objects.create(
            user=User.objects.create_user(
                email="other@example.com",
                password="Other123!",
                name="다른오너",
                confirmed=True,
            ),
            name="외부 매장",
            address="부산",
            max_admin_count=5,
            width=500,
            height=300,
            depth=400,
        )
        response = self._post(self._payload(store_id=outsider_store.id), self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")
        self.assertEqual(ProductMaster.objects.count(), 0)

    def test_soft_deleted_store_returns_404(self):
        """삭제된 매장 — OWNER 라도 STORE_NOT_FOUND (멤버십 helper 정책)."""
        self.store.deleted_at = timezone.now()
        self.store.save(update_fields=["deleted_at"])

        response = self._post(self._payload(), self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "STORE_NOT_FOUND")
        self.assertEqual(ProductMaster.objects.count(), 0)


class ProductListEndpointTests(TestCase):
    """GET /api/v1/products — 매장 단위 공유 카탈로그 조회 (store_products bridge).

    가시성 규칙:
      - master 가 store_products 통해 등록된 매장의 멤버에게만 노출.
      - master 자체엔 store FK 없고 store_products bridge 가 명시적 연결.
      - 양쪽 매장 멤버라도 master 가 한 매장에만 등록되어 있으면 다른 매장 컨텍스트엔
        흘러가지 않음 (StoreMember chain 방식의 leak 해소됨).

    정렬: created_at desc.
    null / soft-delete / asset_3d 정책: 163 와 동일.
    """

    url = "/api/v1/products"

    def setUp(self):
        self.user = User.objects.create_user(
            email="user@example.com",
            password="User1234!",
            name="사용자",
            confirmed=True,
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="Other123!",
            name="다른사용자",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

        # 기본 매장 — 대부분 시나리오의 "본인 소속 매장"
        self.store = self._make_store(name="기본 매장")
        StoreMember.objects.create(
            store=self.store,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )

    def _get(self, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url, **headers)

    def _make_store(self, *, name="DO-GO 매장", owner=None):
        return Store.objects.create(
            user=owner or self.user,
            name=name,
            address="서울 성동구",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )

    def _make_master(
        self, *, store=None, owner=None, image_url=None, width=30, height=12, **extra
    ):
        """master + store_products bridge 한 쌍 생성. 정상 등록 흐름과 동일.

        store 미지정 시 self.store 에 묶음. store=False 명시 시 bridge 안 만듦
        (orphan master — '163 외부 직접 INSERT' 같은 비정상 상태 시뮬레이션).
        """
        master = ProductMaster.objects.create(
            user=owner or self.user,
            image_url=image_url,
            width=width,
            height=height,
            **extra,
        )
        if store is not False:
            StoreProduct.objects.create(
                store=store or self.store, product_master=master
            )
        return master

    # ── 인증 ──────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        response = self._get()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED_USER")

    # ── 빈 목록 ───────────────────────────────────────────────────────

    def test_empty_list_returns_empty_array(self):
        response = self._get(self.access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "상품 목록 조회에 성공했습니다.")
        self.assertEqual(body["data"], {"products": []})

    # ── 단건 + 응답 형식 ──────────────────────────────────────────────

    def test_single_master_with_placeholder_variant(self):
        master = self._make_master(
            image_url="https://s3.../shoe.jpg", width=30, height=12
        )
        variant = ProductVariant.objects.create(product_master=master)

        response = self._get(self.access)
        self.assertEqual(response.status_code, 200)
        products = response.json()["data"]["products"]
        self.assertEqual(len(products), 1)

        item = products[0]
        self.assertEqual(item["id"], master.id)
        self.assertIsNone(item["name"])
        self.assertIsNone(item["price"])
        self.assertIsNone(item["depth"])
        self.assertEqual(item["image_url"], "https://s3.../shoe.jpg")
        self.assertEqual(item["width"], 30)
        self.assertEqual(item["height"], 12)
        self.assertIsNone(item["asset_3d"])
        self.assertEqual(len(item["variants"]), 1)

        v = item["variants"][0]
        self.assertEqual(v["id"], variant.id)
        self.assertIsNone(v["size"])
        self.assertIsNone(v["color"])
        self.assertIsNone(v["sku_code"])
        self.assertIsNone(v["barcode_image_url"])

        # 응답 키 잠금
        self.assertEqual(
            set(item.keys()),
            {
                "id",
                "name",
                "price",
                "image_url",
                "width",
                "height",
                "depth",
                "asset_3d",
                "variants",
            },
        )
        self.assertEqual(
            set(v.keys()),
            {"id", "size", "color", "sku_code", "barcode_image_url"},
        )

    def test_user_can_fill_fields_after_capture(self):
        master = self._make_master(
            name="DO-GO 후드티",
            price=45000,
            depth=5,
            image_url="https://s3.../hood.jpg",
        )
        ProductVariant.objects.create(
            product_master=master,
            size="L",
            color="Black",
            sku_code="HD-BLK-L",
        )

        response = self._get(self.access)
        item = response.json()["data"]["products"][0]
        self.assertEqual(item["name"], "DO-GO 후드티")
        self.assertEqual(item["price"], 45000)
        self.assertEqual(item["depth"], 5)
        v = item["variants"][0]
        self.assertEqual(v["size"], "L")
        self.assertEqual(v["color"], "Black")
        self.assertEqual(v["sku_code"], "HD-BLK-L")

    # ── 정렬 ──────────────────────────────────────────────────────────

    def test_sorted_by_created_at_desc(self):
        old = self._make_master()
        mid = self._make_master()
        new = self._make_master()

        response = self._get(self.access)
        ids = [p["id"] for p in response.json()["data"]["products"]]
        self.assertEqual(ids, [new.id, mid.id, old.id])

    # ── 다중 variants ─────────────────────────────────────────────────

    def test_multiple_variants_under_one_master(self):
        master = self._make_master()
        v1 = ProductVariant.objects.create(product_master=master, size="M")
        v2 = ProductVariant.objects.create(product_master=master, size="L")

        response = self._get(self.access)
        item = response.json()["data"]["products"][0]
        variant_ids = {v["id"] for v in item["variants"]}
        self.assertEqual(variant_ids, {v1.id, v2.id})
        self.assertEqual(len(item["variants"]), 2)

    # ── 매장 단위 가시성 (store_products bridge) ──────────────────────

    def test_teammate_in_same_store_products_are_visible(self):
        """같은 매장 동료가 등록한 master 도 응답에 포함."""
        StoreMember.objects.create(
            store=self.store,
            user=self.other_user,
            role=StoreMember.Role.VMD,
        )
        teammate_product = self._make_master(
            owner=self.other_user,
            image_url="https://s3.../teammate.jpg",
        )
        my_product = self._make_master(image_url="https://s3.../mine.jpg")

        response = self._get(self.access)
        ids = {p["id"] for p in response.json()["data"]["products"]}
        self.assertEqual(ids, {teammate_product.id, my_product.id})

    def test_outsider_store_products_not_visible(self):
        """본인이 멤버 아닌 매장의 master 는 응답에서 제외."""
        outsider_store = self._make_store(name="외부 매장", owner=self.other_user)
        StoreMember.objects.create(
            store=outsider_store,
            user=self.other_user,
            role=StoreMember.Role.OWNER,
        )
        # other_user 가 outsider_store 에 master 등록
        self._make_master(
            store=outsider_store,
            owner=self.other_user,
            image_url="https://s3.../outsider.jpg",
        )
        # 본인은 self.store 에 master 등록
        mine = self._make_master(image_url="https://s3.../mine.jpg")

        response = self._get(self.access)
        ids = [p["id"] for p in response.json()["data"]["products"]]
        self.assertEqual(ids, [mine.id])

    def test_master_without_store_products_invisible(self):
        """store_products bridge 없는 orphan master (비정상 상태) 는 응답에 안 들어감.
        163 정상 흐름에선 이런 row 가 만들어지지 않음."""
        orphan = self._make_master(store=False, image_url="https://s3.../orphan.jpg")  # noqa: F841
        normal = self._make_master(image_url="https://s3.../normal.jpg")

        response = self._get(self.access)
        ids = [p["id"] for p in response.json()["data"]["products"]]
        self.assertEqual(ids, [normal.id])

    def test_bridge_user_does_not_leak_across_stores(self):
        """양쪽 매장 멤버 leak 해소 검증 — 핵심 시나리오.

        시나리오:
          - U(본인)는 매장 X+Y 양쪽 멤버
          - U 가 매장 X 의 fixture 에서 사진 촬영 → master M 등록 (store_products(X, M))
          - V 는 매장 Y 만 멤버 → V 의 GET /products 에 M 이 *안* 보여야 함.
        """
        store_x = self.store  # 본인이 OWNER
        store_y = self._make_store(name="매장 Y")
        StoreMember.objects.create(
            store=store_y,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )
        # V (other_user) 는 매장 Y 만 멤버
        StoreMember.objects.create(
            store=store_y,
            user=self.other_user,
            role=StoreMember.Role.VMD,
        )

        # U 가 매장 X 에 master 등록
        master_in_x = self._make_master(
            store=store_x,
            image_url="https://s3.../store_x.jpg",
        )
        # U 가 매장 Y 에도 master 등록 (이건 V 에게 보여야 함)
        master_in_y = self._make_master(
            store=store_y,
            image_url="https://s3.../store_y.jpg",
        )

        # V (other_user) 의 응답 — 매장 Y 의 master 만 보여야 함
        v_access = str(AccessToken.for_user(self.other_user))
        response = self._get(v_access)
        ids = {p["id"] for p in response.json()["data"]["products"]}
        self.assertEqual(ids, {master_in_y.id})
        self.assertNotIn(master_in_x.id, ids)  # leak 방지 핵심 assertion

    def test_one_master_in_multiple_stores_visible_to_each_member(self):
        """master 1개가 store_products row 2개 통해 두 매장에 등록되면 양쪽 멤버 모두 보임.
        store_products UniqueConstraint(store, master) 이 같은 (store, master) 중복은 막음."""
        store_b = self._make_store(name="매장 B")
        StoreMember.objects.create(
            store=store_b,
            user=self.other_user,
            role=StoreMember.Role.OWNER,
        )

        master = self._make_master(image_url="https://s3.../shared.jpg")  # store_a 매핑
        StoreProduct.objects.create(
            store=store_b, product_master=master
        )  # store_b 추가

        # 본인 (store_a 멤버) 응답
        ids_self = {p["id"] for p in self._get(self.access).json()["data"]["products"]}
        self.assertIn(master.id, ids_self)

        # other_user (store_b 멤버) 응답
        v_access = str(AccessToken.for_user(self.other_user))
        ids_other = {p["id"] for p in self._get(v_access).json()["data"]["products"]}
        self.assertIn(master.id, ids_other)

    # ── 소프트 삭제 ───────────────────────────────────────────────────

    def test_soft_deleted_master_excluded(self):
        alive = self._make_master(image_url="https://s3.../alive.jpg")
        dead = self._make_master(image_url="https://s3.../dead.jpg")
        dead.deleted_at = timezone.now()
        dead.save(update_fields=["deleted_at"])

        response = self._get(self.access)
        ids = [p["id"] for p in response.json()["data"]["products"]]
        self.assertEqual(ids, [alive.id])

    def test_soft_deleted_store_excludes_its_products(self):
        """매장이 소프트 삭제되면 그 매장에 등록된 master 도 응답에서 제외 (다른 활성 매장 매핑 없으면).
        가시성이 store 의 alive 상태에 의존한다는 정책 검증."""
        store_b = self._make_store(name="매장 B (삭제 예정)")
        StoreMember.objects.create(
            store=store_b,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )

        only_in_dead = self._make_master(  # noqa: F841
            store=store_b,
            image_url="https://s3.../dead_store.jpg",
        )
        in_both = self._make_master(image_url="https://s3.../shared.jpg")
        StoreProduct.objects.create(store=store_b, product_master=in_both)

        store_b.deleted_at = timezone.now()
        store_b.save(update_fields=["deleted_at"])

        response = self._get(self.access)
        ids = {p["id"] for p in response.json()["data"]["products"]}
        # in_both 은 self.store 매핑도 있어 살아있어야 함, only_in_dead 는 사라져야 함
        self.assertIn(in_both.id, ids)
        self.assertNotIn(only_in_dead.id, ids)

    def test_soft_deleted_variant_excluded(self):
        master = self._make_master()
        alive_variant = ProductVariant.objects.create(product_master=master, size="M")
        dead_variant = ProductVariant.objects.create(product_master=master, size="L")
        dead_variant.deleted_at = timezone.now()
        dead_variant.save(update_fields=["deleted_at"])

        response = self._get(self.access)
        item = response.json()["data"]["products"][0]
        variant_ids = [v["id"] for v in item["variants"]]
        self.assertEqual(variant_ids, [alive_variant.id])

    # ── asset_3d ──────────────────────────────────────────────────────

    def test_asset_3d_null_when_no_record(self):
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)

        response = self._get(self.access)
        item = response.json()["data"]["products"][0]
        self.assertIsNone(item["asset_3d"])

    def test_asset_3d_populated_when_record_exists(self):
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/test.glb",
        )

        response = self._get(self.access)
        item = response.json()["data"]["products"][0]
        self.assertIsNotNone(item["asset_3d"])
        self.assertEqual(item["asset_3d"]["file_format"], "GLB")
        self.assertIn("test.glb", item["asset_3d"]["model_url"])

    def test_asset_3d_picks_latest_when_multiple(self):
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.OBJ,
            model_url="assets/3d/old.obj",
        )
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/new.glb",
        )

        response = self._get(self.access)
        item = response.json()["data"]["products"][0]
        self.assertEqual(item["asset_3d"]["file_format"], "GLB")


class ProductDetailEndpointTests(TestCase):
    """GET /api/v1/products/{product_id}/variants — 상품 상세 조회.

    스펙 path 가 /products/{id}/variants 로 끝나지만 응답은 master+variants 묶음.
    가시성 정책은 162 (list) 와 동일 — store_products bridge.

    응답 키:
      - master 9개: product_id (alias of id), name, price, image_url,
        width, height, depth, asset_3d, variants
      - variant 5개: variant_id (alias of id), size, color, sku_code, barcode_image_url

    실패 케이스 통합:
      - 미존재 / 다른 매장 / 매장 소프트삭제 / master 소프트삭제 모두 PRODUCT_NOT_FOUND 404
        (ID 프로빙 차단 — stores 의 STORE_NOT_FOUND 패턴과 동일)
    """

    url_tmpl = "/api/v1/products/{product_id}/variants"

    def setUp(self):
        self.user = User.objects.create_user(
            email="user@example.com",
            password="User1234!",
            name="사용자",
            confirmed=True,
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="Other123!",
            name="다른사용자",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

        self.store = self._make_store(name="기본 매장")
        StoreMember.objects.create(
            store=self.store,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )

    def _get(self, product_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(
            self.url_tmpl.format(product_id=product_id),
            **headers,
        )

    def _make_store(self, *, name="DO-GO 매장", owner=None):
        return Store.objects.create(
            user=owner or self.user,
            name=name,
            address="서울 성동구",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )

    def _make_master(
        self, *, store=None, owner=None, image_url=None, width=30, height=12, **extra
    ):
        master = ProductMaster.objects.create(
            user=owner or self.user,
            image_url=image_url,
            width=width,
            height=height,
            **extra,
        )
        if store is not False:
            StoreProduct.objects.create(
                store=store or self.store, product_master=master
            )
        return master

    # ── 인증 ──────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        master = self._make_master()
        response = self._get(master.id)
        self.assertEqual(response.status_code, 401)

    # ── 성공 ───────────────────────────────────────────────────────────

    def test_success_own_master_with_placeholder_variants(self):
        """163 흐름으로 등록된 placeholder 그대로 응답 — null 패턴 검증."""
        master = self._make_master(image_url="https://s3.../shoe.jpg")
        v1 = ProductVariant.objects.create(product_master=master)
        v2 = ProductVariant.objects.create(product_master=master)

        response = self._get(master.id, self.access)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "상품 상세 조회에 성공했습니다.")

        data = body["data"]
        self.assertEqual(data["product_id"], master.id)
        self.assertIsNone(data["name"])
        self.assertIsNone(data["price"])
        self.assertIsNone(data["depth"])
        self.assertEqual(data["image_url"], "https://s3.../shoe.jpg")
        self.assertEqual(data["width"], 30)
        self.assertEqual(data["height"], 12)
        self.assertIsNone(data["asset_3d"])

        variant_ids = {v["variant_id"] for v in data["variants"]}
        self.assertEqual(variant_ids, {v1.id, v2.id})
        self.assertEqual(len(data["variants"]), 2)

        # 응답 키 잠금
        self.assertEqual(
            set(data.keys()),
            {
                "product_id",
                "name",
                "price",
                "image_url",
                "width",
                "height",
                "depth",
                "asset_3d",
                "variants",
            },
        )
        self.assertEqual(
            set(data["variants"][0].keys()),
            {"variant_id", "size", "color", "sku_code", "barcode_image_url"},
        )

    def test_success_user_can_fill_fields(self):
        """사용자가 placeholder 채운 후 조회 — 채운 값 그대로 응답."""
        master = self._make_master(
            name="DO-GO 후드티",
            price=45000,
            depth=5,
            image_url="https://s3.../hood.jpg",
        )
        ProductVariant.objects.create(
            product_master=master,
            size="L",
            color="Grey",
            sku_code="HD-GRY-L",
        )

        response = self._get(master.id, self.access)
        data = response.json()["data"]
        self.assertEqual(data["name"], "DO-GO 후드티")
        self.assertEqual(data["price"], 45000)
        self.assertEqual(data["depth"], 5)

        v = data["variants"][0]
        self.assertEqual(v["size"], "L")
        self.assertEqual(v["color"], "Grey")
        self.assertEqual(v["sku_code"], "HD-GRY-L")

    def test_success_teammate_master_visible(self):
        """같은 매장 동료가 등록한 master 도 상세 조회 가능."""
        StoreMember.objects.create(
            store=self.store,
            user=self.other_user,
            role=StoreMember.Role.VMD,
        )
        master = self._make_master(
            owner=self.other_user,
            image_url="https://s3.../teammate.jpg",
        )
        ProductVariant.objects.create(product_master=master)

        response = self._get(master.id, self.access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["product_id"], master.id)

    # ── 가시성 / 404 ──────────────────────────────────────────────────

    def test_nonexistent_returns_404(self):
        response = self._get(99999, self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_FOUND")

    def test_outsider_master_returns_404(self):
        """본인이 멤버 아닌 매장의 master — PRODUCT_NOT_FOUND (ID 프로빙 차단)."""
        outsider_store = self._make_store(name="외부 매장", owner=self.other_user)
        StoreMember.objects.create(
            store=outsider_store,
            user=self.other_user,
            role=StoreMember.Role.OWNER,
        )
        master = self._make_master(
            store=outsider_store,
            owner=self.other_user,
            image_url="https://s3.../outsider.jpg",
        )

        response = self._get(master.id, self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_FOUND")

    def test_master_without_store_products_returns_404(self):
        """orphan master (store_products bridge 없음) 도 404."""
        orphan = self._make_master(store=False, image_url="https://s3.../orphan.jpg")
        response = self._get(orphan.id, self.access)
        self.assertEqual(response.status_code, 404)

    def test_bridge_user_does_not_leak_in_detail(self):
        """list 와 동일한 leak 정책 검증 — 매장 X 에만 등록된 master 는
        매장 Y 만 멤버인 사용자에게 *상세 조회로도* 안 보임."""
        store_x = self.store
        store_y = self._make_store(name="매장 Y")
        StoreMember.objects.create(
            store=store_y,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )
        StoreMember.objects.create(
            store=store_y,
            user=self.other_user,
            role=StoreMember.Role.VMD,
        )
        master_in_x = self._make_master(store=store_x, image_url="https://s3.../x.jpg")

        v_access = str(AccessToken.for_user(self.other_user))
        response = self._get(master_in_x.id, v_access)
        self.assertEqual(response.status_code, 404)

    def test_soft_deleted_master_returns_404(self):
        master = self._make_master(image_url="https://s3.../alive.jpg")
        master.deleted_at = timezone.now()
        master.save(update_fields=["deleted_at"])

        response = self._get(master.id, self.access)
        self.assertEqual(response.status_code, 404)

    def test_soft_deleted_store_excludes_its_master(self):
        store_b = self._make_store(name="매장 B (삭제 예정)")
        StoreMember.objects.create(
            store=store_b,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )
        only_in_dead = self._make_master(
            store=store_b,
            image_url="https://s3.../dead.jpg",
        )

        store_b.deleted_at = timezone.now()
        store_b.save(update_fields=["deleted_at"])

        response = self._get(only_in_dead.id, self.access)
        self.assertEqual(response.status_code, 404)

    # ── 소프트 삭제된 variant 제외 ────────────────────────────────────

    def test_soft_deleted_variant_excluded(self):
        master = self._make_master()
        alive_v = ProductVariant.objects.create(product_master=master, size="M")
        dead_v = ProductVariant.objects.create(product_master=master, size="L")
        dead_v.deleted_at = timezone.now()
        dead_v.save(update_fields=["deleted_at"])

        response = self._get(master.id, self.access)
        variants = response.json()["data"]["variants"]
        self.assertEqual([v["variant_id"] for v in variants], [alive_v.id])

    # ── asset_3d ──────────────────────────────────────────────────────

    def test_asset_3d_null_when_no_record(self):
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)
        response = self._get(master.id, self.access)
        self.assertIsNone(response.json()["data"]["asset_3d"])

    def test_asset_3d_populated(self):
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/test.glb",
        )

        response = self._get(master.id, self.access)
        data = response.json()["data"]
        self.assertEqual(data["asset_3d"]["file_format"], "GLB")
        self.assertIn("test.glb", data["asset_3d"]["model_url"])

    def test_asset_3d_picks_latest_when_multiple(self):
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.OBJ,
            model_url="assets/3d/old.obj",
        )
        Asset3D.objects.create(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id=master.id,
            file_format=Asset3D.FileFormat.GLB,
            model_url="assets/3d/new.glb",
        )

        response = self._get(master.id, self.access)
        self.assertEqual(response.json()["data"]["asset_3d"]["file_format"], "GLB")


@override_settings(MEDIA_ROOT=_MEDIA_TMP)
class ProductUpdateEndpointTests(TestCase):
    """PATCH /api/v1/products/{product_id} — 상품 부분 수정 + variants bulk sync (multipart).

    167 endpoint(POST /products/{id}/variants) 책임 흡수 — 카메라 스캔 흐름의
    sku/size/color/barcode image 일괄 등록도 본 endpoint 한 번으로 처리.

    Content-Type: multipart/form-data (메타만 수정해도 multipart, image 부분 비움).
    Body:
      - data (form): JSON-encoded ProductUpdateIn
      - images (file array, optional): variants[].image_index 로 매칭

    PATCH 의미론:
      - 메타 (name/price/image_url/width/height/depth) 부분 수정 — 보낸 키만 변경
      - variants 키 부재 → 메타만, variants=[] → 전체 SOFT DELETE,
        variants=[{...}] → 3-rule bulk sync (id 있음 UPDATE, 없음 INSERT, 누락 SOFT DELETE)
      - image_index 키 부재/null → image 변경 X (UPDATE) 또는 image 없음 (INSERT)
      - image_index 값 있음 → images[idx] 를 ImageField 에 저장

    가시성/권한:
      - 162/166 와 동일 store_products bridge — 매장 멤버 누구나 수정 가능 (STAFF 포함)
      - 비가시 마스터 → PRODUCT_NOT_FOUND 404

    검증:
      - cross-master variant id → INVALID_VARIANT_ID 422
      - sku 중복 (request 내부 / 다른 master / 같은 master 다른 row) → SKU_DUPLICATED 409
      - image_index 가 images 길이보다 크면 → INVALID_IMAGE_INDEX 422
      - 한 row 라도 invalid 면 *어떤 변경도 안 일어남* (transaction.atomic + fail-fast)
    """

    url_tmpl = "/api/v1/products/{product_id}"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        self.user = User.objects.create_user(
            email="user@example.com",
            password="User1234!",
            name="사용자",
            confirmed=True,
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="Other123!",
            name="다른사용자",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

        self.store = self._make_store(name="기본 매장")
        StoreMember.objects.create(
            store=self.store,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )

    def _patch(self, product_id, body, access_token=None, *, images=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        # multipart: data form field 에 JSON, images 는 file array.
        # Django test client 의 .patch() default 는 application/octet-stream — 직접
        # encode_multipart + MULTIPART_CONTENT 로 multipart 강제. .post() 와 달리
        # dict 자동 인코딩 X.
        form = {"data": json.dumps(body)}
        if images:
            form["images"] = images
        encoded = encode_multipart(BOUNDARY, form)
        return self.client.patch(
            self.url_tmpl.format(product_id=product_id),
            data=encoded,
            content_type=MULTIPART_CONTENT,
            **headers,
        )

    def _make_store(self, *, name="DO-GO 매장", owner=None):
        return Store.objects.create(
            user=owner or self.user,
            name=name,
            address="서울 성동구",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )

    def _make_master(self, *, store=None, owner=None, **extra):
        master = ProductMaster.objects.create(
            user=owner or self.user,
            width=extra.pop("width", 30),
            height=extra.pop("height", 12),
            **extra,
        )
        if store is not False:
            StoreProduct.objects.create(
                store=store or self.store, product_master=master
            )
        return master

    # ── 인증 ──────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        master = self._make_master()
        response = self._patch(master.id, {"name": "X"})
        self.assertEqual(response.status_code, 401)

    # ── 메타 부분 수정 ────────────────────────────────────────────────

    def test_partial_meta_update_only_changes_sent_fields(self):
        """name 만 보내면 다른 필드 (price, depth 등) 는 그대로."""
        master = self._make_master(name="원본 이름", price=10000, depth=5)
        response = self._patch(master.id, {"name": "수정된 이름"}, self.access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "상품 정보가 성공적으로 수정되었습니다.")

        data = body["data"]
        self.assertEqual(data["product_id"], master.id)
        self.assertEqual(data["name"], "수정된 이름")
        self.assertEqual(data["synced_variants_count"], 0)
        self.assertEqual(data["deleted_variants_count"], 0)
        self.assertIn("updated_at", data)

        master.refresh_from_db()
        self.assertEqual(master.name, "수정된 이름")
        self.assertEqual(master.price, 10000)  # 그대로
        self.assertEqual(master.depth, 5)  # 그대로

        # 응답 키 잠금 — variants 는 165 multipart 흡수 후 응답 포함 (image 검증용)
        self.assertEqual(
            set(data.keys()),
            {
                "product_id",
                "name",
                "synced_variants_count",
                "deleted_variants_count",
                "updated_at",
                "variants",
            },
        )

    def test_can_fill_null_placeholder_fields(self):
        """163 으로 등록된 placeholder (name/price/depth=null) 채우기."""
        master = self._make_master()
        self.assertIsNone(master.name)
        self.assertIsNone(master.price)
        self.assertIsNone(master.depth)

        response = self._patch(
            master.id,
            {
                "name": "DO-GO 후드티",
                "price": 45000,
                "depth": 5,
            },
            self.access,
        )
        self.assertEqual(response.status_code, 200)

        master.refresh_from_db()
        self.assertEqual(master.name, "DO-GO 후드티")
        self.assertEqual(master.price, 45000)
        self.assertEqual(master.depth, 5)

    def test_negative_price_returns_422(self):
        master = self._make_master()
        response = self._patch(master.id, {"price": -1}, self.access)
        self.assertEqual(response.status_code, 422)

    def test_zero_dimension_returns_422(self):
        master = self._make_master()
        response = self._patch(master.id, {"width": 0}, self.access)
        self.assertEqual(response.status_code, 422)

    # ── variants bulk sync ────────────────────────────────────────────

    def test_variants_insert_new_row(self):
        """id 없이 보내면 INSERT — 새 variant 추가."""
        master = self._make_master()
        ProductVariant.objects.create(product_master=master)  # 기존 1건

        response = self._patch(
            master.id,
            {
                "variants": [
                    {"size": "M", "color": "Black", "sku_code": "TS-M"},
                ],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 200)

        # 기존 1건은 누락이라 SOFT DELETE, 신규 1건은 INSERT
        data = response.json()["data"]
        self.assertEqual(data["synced_variants_count"], 1)
        self.assertEqual(data["deleted_variants_count"], 1)

        alive = ProductVariant.objects.alive().filter(product_master=master)
        self.assertEqual(alive.count(), 1)
        self.assertEqual(alive.first().sku_code, "TS-M")

    def test_variants_update_existing_row(self):
        """id 있으면 UPDATE — 보낸 필드만."""
        master = self._make_master()
        v = ProductVariant.objects.create(
            product_master=master, size="M", color="Black"
        )

        response = self._patch(
            master.id,
            {
                "variants": [{"id": v.id, "color": "Red"}],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 200)

        data = response.json()["data"]
        self.assertEqual(data["synced_variants_count"], 1)
        self.assertEqual(data["deleted_variants_count"], 0)

        v.refresh_from_db()
        self.assertEqual(v.color, "Red")
        self.assertEqual(v.size, "M")  # 안 보낸 필드 — 그대로

    def test_variants_soft_delete_missing_ids(self):
        """alive 중 array 에 누락된 id → SOFT DELETE."""
        master = self._make_master()
        keep = ProductVariant.objects.create(product_master=master, size="M")
        drop = ProductVariant.objects.create(product_master=master, size="L")

        response = self._patch(
            master.id,
            {
                "variants": [{"id": keep.id}],  # drop 은 누락
            },
            self.access,
        )
        self.assertEqual(response.status_code, 200)

        data = response.json()["data"]
        self.assertEqual(data["deleted_variants_count"], 1)

        keep.refresh_from_db()
        drop.refresh_from_db()
        self.assertIsNone(keep.deleted_at)
        self.assertIsNotNone(drop.deleted_at)

    def test_empty_variants_array_soft_deletes_all(self):
        """variants=[] 면 모든 alive variant SOFT DELETE."""
        master = self._make_master()
        v1 = ProductVariant.objects.create(product_master=master, size="M")
        v2 = ProductVariant.objects.create(product_master=master, size="L")

        response = self._patch(master.id, {"variants": []}, self.access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["deleted_variants_count"], 2)

        for v in (v1, v2):
            v.refresh_from_db()
            self.assertIsNotNone(v.deleted_at)

    def test_variants_key_absent_keeps_variants(self):
        """variants 키 자체 부재 → variants 건드리지 않고 메타만 수정."""
        master = self._make_master(name="원본")
        v = ProductVariant.objects.create(product_master=master, size="M")

        response = self._patch(master.id, {"name": "새 이름"}, self.access)
        self.assertEqual(response.status_code, 200)

        data = response.json()["data"]
        self.assertEqual(data["synced_variants_count"], 0)
        self.assertEqual(data["deleted_variants_count"], 0)

        v.refresh_from_db()
        self.assertIsNone(v.deleted_at)

    def test_mixed_insert_update_delete(self):
        """3-rule 한 번에 적용 — UPDATE 1 + INSERT 1 + SOFT DELETE 1."""
        master = self._make_master()
        keep = ProductVariant.objects.create(product_master=master, size="M")
        drop = ProductVariant.objects.create(product_master=master, size="L")

        response = self._patch(
            master.id,
            {
                "variants": [
                    {"id": keep.id, "size": "M2"},  # UPDATE
                    {
                        "size": "XL",
                        "color": "Red",
                        "sku_code": "X1",
                    },  # INSERT (drop 은 누락 → SOFT DELETE)
                ],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 200)

        data = response.json()["data"]
        self.assertEqual(data["synced_variants_count"], 2)
        self.assertEqual(data["deleted_variants_count"], 1)

        keep.refresh_from_db()
        self.assertEqual(keep.size, "M2")
        drop.refresh_from_db()
        self.assertIsNotNone(drop.deleted_at)

    def test_cross_master_variant_id_returns_422(self):
        """다른 master 의 variant id 보내면 INVALID_VARIANT_ID."""
        master = self._make_master()
        other_master = self._make_master()
        other_variant = ProductVariant.objects.create(product_master=other_master)

        response = self._patch(
            master.id,
            {
                "variants": [{"id": other_variant.id, "size": "M"}],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_VARIANT_ID")

        # 어떤 변경도 일어나지 않음 — atomic 검증
        other_variant.refresh_from_db()
        self.assertIsNone(other_variant.size)

    def test_soft_deleted_variant_id_returns_422(self):
        """이미 SOFT DELETE 된 variant id 재참조 거부."""
        master = self._make_master()
        dead = ProductVariant.objects.create(product_master=master, size="M")
        dead.deleted_at = timezone.now()
        dead.save(update_fields=["deleted_at"])

        response = self._patch(
            master.id,
            {
                "variants": [{"id": dead.id, "color": "X"}],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_VARIANT_ID")

    # ── sku 중복 ──────────────────────────────────────────────────────

    def test_sku_duplicate_within_request_returns_409(self):
        """request 내부에서 같은 sku_code 두 번 → SKU_DUPLICATED."""
        master = self._make_master()
        response = self._patch(
            master.id,
            {
                "variants": [
                    {"size": "M", "sku_code": "DUP-1"},
                    {"size": "L", "sku_code": "DUP-1"},
                ],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SKU_DUPLICATED")

        # 어떤 INSERT 도 일어나지 않음
        self.assertEqual(
            ProductVariant.objects.filter(product_master=master).count(),
            0,
        )

    def test_sku_conflict_with_other_master_returns_409(self):
        """다른 master 의 alive variant 와 sku_code 충돌."""
        master = self._make_master()
        other_master = self._make_master()
        ProductVariant.objects.create(product_master=other_master, sku_code="GLOBAL-1")

        response = self._patch(
            master.id,
            {
                "variants": [{"sku_code": "GLOBAL-1", "size": "M"}],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SKU_DUPLICATED")

    def test_sku_conflict_within_same_master_returns_409(self):
        """같은 master 내 *다른* alive variant 에 같은 sku 가 있으면 거부."""
        master = self._make_master()
        v1 = ProductVariant.objects.create(product_master=master, sku_code="MINE-1")
        v2 = ProductVariant.objects.create(product_master=master, sku_code="MINE-2")

        # v2 의 sku 를 v1 과 같게 변경 시도
        response = self._patch(
            master.id,
            {
                "variants": [
                    {"id": v1.id},
                    {"id": v2.id, "sku_code": "MINE-1"},
                ],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 409)

    def test_can_keep_own_sku_code_during_update(self):
        """자기 자신의 sku_code 그대로 보내는 건 충돌 X."""
        master = self._make_master()
        v = ProductVariant.objects.create(product_master=master, sku_code="MINE-1")

        response = self._patch(
            master.id,
            {
                "variants": [{"id": v.id, "sku_code": "MINE-1", "size": "M"}],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 200)

    # ── 가시성 / 404 ──────────────────────────────────────────────────

    def test_nonexistent_returns_404(self):
        response = self._patch(99999, {"name": "X"}, self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_FOUND")

    def test_outsider_master_returns_404(self):
        outsider_store = self._make_store(name="외부 매장", owner=self.other_user)
        StoreMember.objects.create(
            store=outsider_store,
            user=self.other_user,
            role=StoreMember.Role.OWNER,
        )
        master = self._make_master(store=outsider_store, owner=self.other_user)

        response = self._patch(master.id, {"name": "X"}, self.access)
        self.assertEqual(response.status_code, 404)

    def test_staff_can_update_per_shared_catalog_policy(self):
        """매장 멤버 누구나 수정 가능 — STAFF 도 OK (165 결정)."""
        StoreMember.objects.create(
            store=self.store,
            user=self.other_user,
            role=StoreMember.Role.STAFF,
        )
        master = self._make_master()

        staff_access = str(AccessToken.for_user(self.other_user))
        response = self._patch(master.id, {"name": "STAFF 수정"}, staff_access)
        self.assertEqual(response.status_code, 200)

    def test_soft_deleted_master_returns_404(self):
        master = self._make_master()
        master.deleted_at = timezone.now()
        master.save(update_fields=["deleted_at"])

        response = self._patch(master.id, {"name": "X"}, self.access)
        self.assertEqual(response.status_code, 404)

    # ── 167 흡수: image 업로드 (multipart) ────────────────────────────

    def test_image_upload_on_insert_new_variant(self):
        """INSERT 시 image_index 매칭으로 ImageField 저장."""
        master = self._make_master()
        img = _make_image("a.jpg")
        response = self._patch(
            master.id,
            {
                "variants": [
                    {"sku_code": "SKU-NEW", "size": "M", "image_index": 0},
                ],
            },
            self.access,
            images=[img],
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["synced_variants_count"], 1)
        self.assertEqual(len(data["variants"]), 1)
        self.assertIsNotNone(data["variants"][0]["barcode_image_url"])
        self.assertIn(
            "/media/products/barcode/", data["variants"][0]["barcode_image_url"]
        )

        # DB 검증
        v = ProductVariant.objects.get(sku_code="SKU-NEW")
        self.assertTrue(bool(v.barcode_image_url))

    def test_image_upload_on_update_existing_variant(self):
        """UPDATE 시 기존 variant 의 image 채우기 (placeholder → 실제 image)."""
        master = self._make_master()
        v = ProductVariant.objects.create(product_master=master)  # placeholder
        self.assertFalse(bool(v.barcode_image_url))

        img = _make_image("b.jpg")
        response = self._patch(
            master.id,
            {
                "variants": [
                    {"id": v.id, "sku_code": "FILLED", "size": "L", "image_index": 0},
                ],
            },
            self.access,
            images=[img],
        )

        self.assertEqual(response.status_code, 200)
        v.refresh_from_db()
        self.assertEqual(v.sku_code, "FILLED")
        self.assertEqual(v.size, "L")
        self.assertTrue(bool(v.barcode_image_url))

    def test_no_image_index_keeps_existing_image(self):
        """UPDATE 시 image_index 누락 → 기존 image 그대로 유지."""
        master = self._make_master()
        v = ProductVariant.objects.create(product_master=master, sku_code="K")
        v.barcode_image_url = _make_image("orig.jpg")
        v.save()
        original_path = v.barcode_image_url.name

        # image_index 누락 — sku_code 만 변경
        response = self._patch(
            master.id,
            {
                "variants": [{"id": v.id, "sku_code": "K2"}],
            },
            self.access,
        )

        self.assertEqual(response.status_code, 200)
        v.refresh_from_db()
        self.assertEqual(v.sku_code, "K2")
        # image 그대로 — 파일명 변경 X
        self.assertEqual(v.barcode_image_url.name, original_path)

    def test_image_index_out_of_range_returns_422(self):
        master = self._make_master()
        response = self._patch(
            master.id,
            {
                "variants": [{"sku_code": "X", "image_index": 5}],
            },
            self.access,
            images=[_make_image()],
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_IMAGE_INDEX")
        # fail-fast — INSERT 일어나지 않음
        self.assertEqual(
            ProductVariant.objects.filter(product_master=master).count(),
            0,
        )

    def test_image_index_when_no_images_sent_returns_422(self):
        """image_index 있지만 images 자체 안 보냄 → 범위 밖과 동일 (idx >= 0 = len(0))."""
        master = self._make_master()
        response = self._patch(
            master.id,
            {
                "variants": [{"sku_code": "X", "image_index": 0}],
            },
            self.access,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INVALID_IMAGE_INDEX")

    def test_negative_image_index_returns_422(self):
        """pydantic ge=0 검증."""
        master = self._make_master()
        response = self._patch(
            master.id,
            {
                "variants": [{"sku_code": "X", "image_index": -1}],
            },
            self.access,
        )
        self.assertEqual(response.status_code, 422)

    def test_sparse_image_mapping(self):
        """variants 3개 중 0번/2번에만 image, 1번은 image 없음."""
        master = self._make_master()
        img_a = _make_image("a.jpg")
        img_c = _make_image("c.jpg")

        response = self._patch(
            master.id,
            {
                "variants": [
                    {"sku_code": "A", "image_index": 0},
                    {"sku_code": "B"},  # image 없음
                    {"sku_code": "C", "image_index": 1},
                ],
            },
            self.access,
            images=[img_a, img_c],
        )

        self.assertEqual(response.status_code, 200)
        variants = response.json()["data"]["variants"]
        self.assertEqual(len(variants), 3)
        self.assertIsNotNone(variants[0]["barcode_image_url"])  # A
        self.assertIsNone(variants[1]["barcode_image_url"])  # B
        self.assertIsNotNone(variants[2]["barcode_image_url"])  # C

    def test_meta_only_update_works_without_images(self):
        """image 안 보내도 메타만 multipart 로 수정 가능."""
        master = self._make_master(name="Old", price=1000)
        response = self._patch(master.id, {"name": "New", "price": 2000}, self.access)

        self.assertEqual(response.status_code, 200)
        master.refresh_from_db()
        self.assertEqual(master.name, "New")
        self.assertEqual(master.price, 2000)

    def test_response_variants_has_expected_keys(self):
        """응답 variants[i] 의 키 잠금."""
        master = self._make_master()
        response = self._patch(
            master.id,
            {
                "variants": [{"sku_code": "K"}],
            },
            self.access,
        )
        v = response.json()["data"]["variants"][0]
        self.assertEqual(
            set(v.keys()),
            {"variant_id", "size", "color", "sku_code", "barcode_image_url"},
        )


class ProductDeleteEndpointTests(TestCase):
    """DELETE /api/v1/products/{product_id} — 상품 삭제 (4단계 트랜잭션).

    스펙 line 19~25:
      1. master soft delete (deleted_at 세팅)
      2. variants 일괄 soft delete
      3. store_products HARD delete (재할당 방지)
      4. store_inventories HARD delete

    권한: 162/166/165 와 동일 — 매장 멤버 누구나 (STAFF 포함). 비가시 → PRODUCT_NOT_FOUND
    404. 스펙 line 79 의 ACCESS_DENIED 는 안 씀 (카탈로그 공유 모델 일관 — divergence).
    """

    url_tmpl = "/api/v1/products/{product_id}"

    def setUp(self):
        self.user = User.objects.create_user(
            email="user@example.com",
            password="User1234!",
            name="사용자",
            confirmed=True,
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="Other123!",
            name="다른사용자",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

        self.store = self._make_store(name="기본 매장")
        StoreMember.objects.create(
            store=self.store,
            user=self.user,
            role=StoreMember.Role.OWNER,
        )

    def _delete(self, product_id, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.delete(
            self.url_tmpl.format(product_id=product_id),
            **headers,
        )

    def _make_store(self, *, name="DO-GO 매장", owner=None):
        return Store.objects.create(
            user=owner or self.user,
            name=name,
            address="서울 성동구",
            max_admin_count=5,
            width=1000,
            height=300,
            depth=800,
        )

    def _make_master(self, *, store=None, owner=None, **extra):
        master = ProductMaster.objects.create(
            user=owner or self.user,
            width=extra.pop("width", 30),
            height=extra.pop("height", 12),
            **extra,
        )
        if store is not False:
            StoreProduct.objects.create(
                store=store or self.store, product_master=master
            )
        return master

    # ── 인증 ──────────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self):
        master = self._make_master()
        response = self._delete(master.id)
        self.assertEqual(response.status_code, 401)

    # ── 정상 삭제 (4단계 모두 검증) ───────────────────────────────────

    def test_success_4_step_deletion(self):
        """master soft + variants soft + store_products hard + store_inventories hard."""
        master = self._make_master()
        v1 = ProductVariant.objects.create(product_master=master, size="M")
        v2 = ProductVariant.objects.create(product_master=master, size="L")
        # 매장 재고 row 도 만들어 cascade 검증
        StoreInventory.objects.create(store=self.store, variant=v1, stock_quantity=10)
        StoreInventory.objects.create(store=self.store, variant=v2, stock_quantity=20)

        response = self._delete(master.id, self.access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "상품이 성공적으로 삭제 처리되었습니다.")

        data = body["data"]
        self.assertEqual(data["deleted_product_id"], master.id)
        self.assertEqual(data["deleted_variants_count"], 2)
        self.assertEqual(
            set(data.keys()),
            {"deleted_product_id", "deleted_variants_count"},
        )

        # 1) master soft delete — row 살아있고 deleted_at 세팅
        master.refresh_from_db()
        self.assertIsNotNone(master.deleted_at)

        # 2) variants soft delete — row 살아있고 deleted_at 세팅
        for v in (v1, v2):
            v.refresh_from_db()
            self.assertIsNotNone(v.deleted_at)

        # 3) store_products hard delete — row 자체 사라짐
        self.assertFalse(StoreProduct.objects.filter(product_master=master).exists())

        # 4) store_inventories hard delete — row 자체 사라짐
        self.assertFalse(
            StoreInventory.objects.filter(variant__product_master=master).exists()
        )

    def test_already_soft_deleted_variant_not_recounted(self):
        """이미 soft delete 된 variant 는 deleted_variants_count 에 포함 X."""
        master = self._make_master()
        ProductVariant.objects.create(product_master=master, size="M")
        already_dead = ProductVariant.objects.create(product_master=master, size="L")
        already_dead.deleted_at = timezone.now()
        already_dead.save(update_fields=["deleted_at"])

        response = self._delete(master.id, self.access)
        self.assertEqual(response.status_code, 200)
        # alive 1건만 카운트
        self.assertEqual(response.json()["data"]["deleted_variants_count"], 1)

    def test_master_with_no_variants_count_zero(self):
        master = self._make_master()
        response = self._delete(master.id, self.access)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["deleted_variants_count"], 0)

    # ── 가시성 / 404 ──────────────────────────────────────────────────

    def test_nonexistent_returns_404(self):
        response = self._delete(99999, self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_FOUND")

    def test_already_soft_deleted_returns_404(self):
        """master 가 이미 soft delete 된 상태에서 다시 삭제 시도 → 404."""
        master = self._make_master()
        master.deleted_at = timezone.now()
        master.save(update_fields=["deleted_at"])

        response = self._delete(master.id, self.access)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "PRODUCT_NOT_FOUND")

    def test_outsider_master_returns_404(self):
        """다른 매장의 master — PRODUCT_NOT_FOUND (ID 프로빙 차단)."""
        outsider_store = self._make_store(name="외부 매장", owner=self.other_user)
        StoreMember.objects.create(
            store=outsider_store,
            user=self.other_user,
            role=StoreMember.Role.OWNER,
        )
        master = self._make_master(store=outsider_store, owner=self.other_user)

        response = self._delete(master.id, self.access)
        self.assertEqual(response.status_code, 404)

        # 부분 변경도 없음 — 보존
        master.refresh_from_db()
        self.assertIsNone(master.deleted_at)

    def test_master_without_store_products_returns_404(self):
        """orphan master (bridge 없음) 도 가시성 정책상 404."""
        orphan = self._make_master(store=False)
        response = self._delete(orphan.id, self.access)
        self.assertEqual(response.status_code, 404)

    # ── STAFF 권한 ────────────────────────────────────────────────────

    def test_staff_can_delete_per_shared_catalog_policy(self):
        """매장 멤버 누구나 — STAFF 도 삭제 가능 (165 정책 일관)."""
        StoreMember.objects.create(
            store=self.store,
            user=self.other_user,
            role=StoreMember.Role.STAFF,
        )
        master = self._make_master()

        staff_access = str(AccessToken.for_user(self.other_user))
        response = self._delete(master.id, staff_access)
        self.assertEqual(response.status_code, 200)

        master.refresh_from_db()
        self.assertIsNotNone(master.deleted_at)

    # ── 트랜잭션 격리 ─────────────────────────────────────────────────

    def test_other_master_unaffected(self):
        """삭제 대상 master 의 데이터만 처리 — 다른 master 는 무관."""
        keep = self._make_master()
        keep_v = ProductVariant.objects.create(product_master=keep, size="M")

        target = self._make_master()
        ProductVariant.objects.create(product_master=target, size="L")

        response = self._delete(target.id, self.access)
        self.assertEqual(response.status_code, 200)

        # keep 측은 그대로
        keep.refresh_from_db()
        keep_v.refresh_from_db()
        self.assertIsNone(keep.deleted_at)
        self.assertIsNone(keep_v.deleted_at)
        self.assertTrue(StoreProduct.objects.filter(product_master=keep).exists())
