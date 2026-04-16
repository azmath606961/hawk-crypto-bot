"""Goal timeline calculator — HAWK Crypto Bot"""
import math

# Compound monthly rates from actual 2yr backtests (GBP 500 start)
r_eth = (1.83)  ** (1/24) - 1   # ETH 1h +83%
r_btc = (1.573) ** (1/24) - 1   # BTC 4h +57.3%
r_sol = (1.219) ** (1/24) - 1   # SOL 4h +21.9%

r_eth_btc     = r_eth + r_btc
r_eth_btc_sol = r_eth + r_btc + r_sol


def months_to(start, end, r):
    return math.log(end / start) / math.log(1 + r)


def fmt(m):
    y, mo = divmod(int(round(m)), 12)
    if y == 0:
        return f"{mo}m"
    if mo == 0:
        return f"{y}y"
    return f"{y}y {mo}m"


print("\nMonthly compound rates (from actual 24-month backtests):")
print(f"  ETH 1h  : {r_eth*100:.2f}%/mo")
print(f"  BTC 4h  : {r_btc*100:.2f}%/mo")
print(f"  SOL 4h  : {r_sol*100:.2f}%/mo")
print()

scenarios = [
    ("ETH 1h only",         r_eth,         0.7),
    ("ETH 1h + BTC 4h",     r_eth_btc,     1.1),
    ("ETH+BTC+SOL 4h",      r_eth_btc_sol, 1.5),
]

print("GBP 500 -> GBP 100,000 ROADMAP")
print("=" * 70)
header = f"  {'Scenario':<22}  {'Rate/mo':>7}  {'T/Day':>5}  {'500->1k':>7}  {'1k->10k':>8}  {'10k->100k':>10}  {'TOTAL':>8}"
print(header)
print("-" * 70)

for name, r, tpd in scenarios:
    a     = fmt(months_to(500,    1_000,   r))
    b     = fmt(months_to(1_000,  10_000,  r))
    c     = fmt(months_to(10_000, 100_000, r))
    total = fmt(months_to(500,    100_000, r))
    print(f"  {name:<22}  {r*100:>6.2f}%  {tpd:>5.1f}  {a:>7}  {b:>8}  {c:>10}  {total:>8}")

print()
print("Current phase : Paper trading (0 real trades yet)")
print("Go-live rule  : 30+ paper trades with confirmed positive EV (R7)")
print()

print("Detailed milestones for best case (ETH+BTC+SOL, ~5.27%/mo):")
r = r_eth_btc_sol
for target in [1_000, 2_000, 5_000, 10_000, 25_000, 50_000, 100_000]:
    m = months_to(500, target, r)
    print(f"  GBP {target:>7,}  :  {fmt(m):>7}  (around month {int(m)+1})")
print()
