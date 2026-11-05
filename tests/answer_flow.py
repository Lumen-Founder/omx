"""Turn the old fixed 8-answer eligibility sequence into the shorter flow onboarding asks now.

old: [entity?, residence, residence region, current country, current region, citizenships, vpn?, terms?]
new: entity? -> residence -> (region only for CA/UA) -> "physically in <residence> now?" ->
     (if no: current country, region only for CA/UA) -> citizenships (blank = residence) -> vpn? -> terms?
"""
from omx.eligibility import REGION_REQUIRED_COUNTRIES


def flow(old):
    entity, res, res_region, here, here_region, cits, vpn, terms = old
    out = [entity, res]
    if res.upper() in REGION_REQUIRED_COUNTRIES:
        out.append(res_region)
    if here.upper() == res.upper() and here_region.upper() == res_region.upper():
        out.append("yes")
    else:
        out += ["no", here] + ([here_region] if here.upper() in REGION_REQUIRED_COUNTRIES else [])
    return out + [cits, vpn, terms]
