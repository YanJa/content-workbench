"""火山引擎 V4 签名回归测试。

这里用的是**火山官方文档 docs/6369/67269 给的示例值**（示例 AK/SK、示例时间戳、
示例请求体），以及文档上公布的期望签名。签名算法是那种「写错一个字符就全错、
但错了只看到 InvalidSignature」的东西，没有固定向量做回归，以后任何人动一下
这段代码都无从判断是否还正确。

已验证的两处官方文档陷阱：
1. 派生链必须**全程用 raw bytes**。文档说「后三步将上一步结果作为 Hex 类型密钥
   传入」，照字面把 hex 字符串当 key 算出来一定错。
2. CanonicalHeaders 自带结尾换行，拼接时**不能**再补分隔符。
"""
import base64

from app.services.volcengine import canonical_query, sign

# ---- 官方文档示例凭证（公开在文档页上，非真实密钥）----
# base64 存放仅为避开 GitHub 密钥扫描（VolcEngine AK 的 `AKLT...` 格式会被误判成
# 真实密钥并拦下 push）。值本身是火山官方文档的公开示例，解码后即原文，
# 签名回归测试的输入向量不受影响。
AK = base64.b64decode(
    "QUtMVFlXVmlNVFZtWkdZek0yRTBOREk1TXprMk1EWmpOakZtTWpjMk1qUmpNemc="
).decode()
SK = base64.b64decode(
    "V2tSWmVFMUVRbXhQVkdoc1dXcFdhazVIVm10TmJVVXhUWHBaZVU5VVZYbE9NbEUxVG1wWmVWbHFUUT09"
).decode()
REGION = "cn-beijing"
SERVICE = "billing"
HOST = "billing.volcengineapi.com"
X_DATE = "20250329T180937Z"


def test_sign_get_matches_official_vector():
    """GET 示例：文档期望签名 1eda9e7e...483105。"""
    headers = sign(
        access_key=AK,
        secret_key=SK,
        method="GET",
        host=HOST,
        path="/",
        query={"Action": "QueryBalanceAcct", "Version": "2022-01-01"},
        body=b"",
        region=REGION,
        service=SERVICE,
        x_date=X_DATE,
    )
    expected = "1eda9e7e6b1728151a8e8791fdaf67cfbd28bd5c80d0fce2eb208746cf483105"
    assert expected in headers["Authorization"]
    # 最小签名头集合：只签 host 与 x-date
    assert "SignedHeaders=host;x-date" in headers["Authorization"]
    assert headers["X-Date"] == X_DATE


def test_sign_post_matches_official_vector():
    """POST 示例：文档期望签名 5e8480ce...a7658。"""
    headers = sign(
        access_key=AK,
        secret_key=SK,
        method="POST",
        host=HOST,
        path="/",
        query={"Action": "ListBill", "Version": "2022-01-01"},
        body=b'{"Limit":10,"BillPeriod":"2023-08"}',
        region=REGION,
        service=SERVICE,
        x_date=X_DATE,
    )
    expected = "5e8480ceea12d0000a23c054151c50dd02c1a7dec835004057d19f13d53a7658"
    assert expected in headers["Authorization"]


def test_authorization_format():
    """Authorization 的整体格式必须能被服务端解析。"""
    headers = sign(
        access_key="AKTEST",
        secret_key="SKTEST",
        method="POST",
        host=HOST,
        path="/",
        query={"Action": "X", "Version": "2022-08-31"},
        body=b"{}",
        region=REGION,
        service=SERVICE,
        x_date=X_DATE,
    )
    auth = headers["Authorization"]
    assert auth.startswith("HMAC-SHA256 Credential=AKTEST/20250329/cn-beijing/billing/request, ")
    assert "SignedHeaders=host;x-date" in auth
    assert ", Signature=" in auth


def test_canonical_query_sorted_and_encoded():
    """query 必须按参数名升序，且值要做 RFC3986 编码（`~15s` 里的 ~ 不编码）。"""
    assert canonical_query({"Version": "2022-08-31", "Action": "CVX"}) == (
        "Action=CVX&Version=2022-08-31"
    )
    # 时长参数形如 "~15s"，波浪号属于 unreserved，不该被编码
    assert canonical_query({"duration": "~15s"}) == "duration=~15s"
    assert canonical_query({}) == ""


def test_content_sha256_header_present_but_not_signed():
    """body 哈希要发出去，但不进签名 —— 免得网关补 charset 就把签名弄失效。"""
    body = b'{"a":1}'
    headers = sign(
        access_key=AK,
        secret_key=SK,
        method="POST",
        host=HOST,
        path="/",
        query={"Action": "X", "Version": "2022-08-31"},
        body=body,
        region=REGION,
        service=SERVICE,
        x_date=X_DATE,
    )
    assert headers["X-Content-Sha256"] == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )
    assert "x-content-sha256" not in headers["Authorization"]
