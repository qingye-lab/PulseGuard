"""二手车探活公共方法：列表取样、签名依赖与基础信息提取。"""

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://cartech.autohome.com.cn",
    "Referer": "https://cartech.autohome.com.cn/",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
}
LIST_URL = "https://cartechmall.autohome.com.cn/newmall/product/list"
SIGN_URL = "https://cartechmall.autohome.com.cn/car-mall/usedcar/sign"
CARINFO_URL = "https://cartechmall.autohome.com.cn/car-mall/cartech/getcarinfo"


async def _request_json(ctx, label, method, url, **kwargs):
    try:
        if method == "POST":
            response = await ctx.http.post(url, headers=HEADERS, **kwargs)
        else:
            response = await ctx.http.get(url, headers=HEADERS, **kwargs)
    except Exception as exc:
        ctx.fail("依赖失败：" + label + " 请求失败：" + str(exc))
    if response.status_code != 200:
        ctx.fail("依赖失败：" + label + " HTTP " + str(response.status_code))
    try:
        payload = response.json()
    except ValueError:
        ctx.fail("依赖失败：" + label + " 响应不是合法 JSON")
    if not isinstance(payload, dict):
        ctx.fail("依赖失败：" + label + " 响应不是对象")
    return payload


def list_params(ctx):
    seed = int(asyncio.get_running_loop().time() * 1000000)
    raw = ("%032x" % seed)[-32:]
    return {
        "pageNumber": 1, "deviceId": "", "pageSize": 20,
        "reqId": str(seed), "cityId": "110100", "carAgeList": "0_0",
        "sortType": 0, "price": "0_0", "carMileageList": "0_0",
        "fuelTypeDetailList": 0, "displacementList": "0_0", "placeList": 0,
        "structureTypeIdList": 0, "seatList": 0, "batteryLifeList": "0_0",
        "engineTypeList": -1, "tagCodeList": "calc_depreciate_car_tag",
        "batteryTypeList": 0, "pcpopclub": "",
        "sessionId": raw[:8] + "-" + raw[8:12] + "-" + raw[12:16] + "-" + raw[16:20] + "-" + raw[20:],
    }


async def list_sample(ctx):
    payload = await _request_json(ctx, "列表", "GET", LIST_URL, params=list_params(ctx))
    if payload.get("returncode") != 0:
        ctx.fail("依赖失败：列表 returncode 不为 0")
    result = payload.get("result")
    records = result.get("records") if isinstance(result, dict) else None
    if not isinstance(records, list):
        ctx.fail("依赖失败：列表 result.records 不是数组")
    sample = None
    for candidate in records:
        if isinstance(candidate, dict) and candidate.get("spuId") and candidate.get("skuId"):
            sample = candidate
            break
    if sample is None:
        ctx.fail("依赖失败：列表 records 为空或没有可用 spuId/skuId 车源")
    spec = sample.get("vehicleSpecDetail")
    used = sample.get("usedCarDetail")
    if not isinstance(spec, dict):
        spec = {}
    if not isinstance(used, dict):
        used = {}
    return {
        "spu": str(sample.get("spuId") or ""),
        "sku": str(sample.get("skuId") or ""),
        "series": str(spec.get("seriesId") or ""),
        "spec": str(spec.get("specId") or ""),
        "city": str(used.get("cityId") or "110100"),
        "price": str(sample.get("goodsPrice") or ""),
    }


def signature_body(ctx, sample):
    return {
        "skuid": sample["sku"], "spuid": sample["spu"], "deviceid": "",
        "ucuserauth": "", "sourceid": "113819,010", "app_v": "1.0.0", "_appid": "xls.ios",
    }


async def sign_for(ctx, label, data):
    payload = await _request_json(ctx, label + "签名", "POST", SIGN_URL, json=data)
    if payload.get("returncode") != 0:
        ctx.fail("依赖失败：" + label + " 签名 returncode 不为 0")
    value = payload.get("result")
    if not isinstance(value, str) or not value.strip():
        ctx.fail("依赖失败：" + label + " 签名 result 为空")
    return value


async def fetch_car_info(ctx, sample, required_fields=()):
    sign_data = signature_body(ctx, sample)
    sign = await sign_for(ctx, "getcarinfo", sign_data)
    payload = await _request_json(ctx, "getcarinfo", "GET", CARINFO_URL, params={**sign_data, "_sign": sign})
    if payload.get("returncode") != 0:
        ctx.fail("依赖失败：getcarinfo returncode 不为 0")
    result = payload.get("result")
    if not isinstance(result, dict):
        ctx.fail("依赖失败：getcarinfo result 不是对象")
    missing = [field for field in required_fields if result.get(field) in (None, "")]
    if missing:
        ctx.fail("依赖失败：getcarinfo 缺少 " + ", ".join(missing))
    return result


def as_int(ctx, value, label):
    try:
        return int(value)
    except (TypeError, ValueError):
        ctx.fail("依赖失败：" + label + " 不是数字")


__all__ = (
    "HEADERS", "LIST_URL", "SIGN_URL", "CARINFO_URL",
    "list_params", "list_sample", "signature_body", "sign_for", "fetch_car_info", "as_int",
)
