"""本地期刊指标：JCR 影响因子 + 中科院分区（内置参考值，可手工或脚本更新）。

IF 取自 hitfyd/ShowJCR 的 JCR2025 表（commit 2557e18，该表于 2026-06-17 发布），
按 ISSN/eISSN 或完整刊名精确匹配当前订阅期刊后写入内置表。
- 新增订阅期刊的 IF 与分区来自同一上游版本，口径一致。
- 原有期刊只更新 IF；其 cas_zone 沿用旧值，未随本次上游版本复核，
  需要更新分区时请单独核对 FQBJCR2025 后再改，避免两套版本混用而不自知。
名称必须与 articles.journal 一致；找不到对应记录时留空，不套用他刊数值。
"""

# 上一版中实际发生变化的内置记录。升级时仅当数据库仍逐字段等于这些值，
# 才自动替换成 SEED_METRICS；用户改动或显式清空任一字段都会阻止覆盖。
PREVIOUS_SEED_METRICS: dict[str, dict] = {
    "Angew. Chem. Int. Ed.": {"full_name": "Angewandte Chemie International Edition", "if_value": 16.6, "cas_zone": 1, "issn": "1433-7851"},
    "Nature Chemistry": {"full_name": "Nature Chemistry", "if_value": 23.2, "cas_zone": 1, "issn": "1755-4330"},
    "Chemical Science": {"full_name": "Chemical Science", "if_value": 7.6, "cas_zone": 1, "issn": "2041-6520"},
    "Phys. Rev. Letters (PRL)": {"full_name": "Physical Review Letters", "if_value": 8.6, "cas_zone": 1, "issn": "0031-9007"},
    "Phys. Rev. B": {"full_name": "Physical Review B", "if_value": 3.7, "cas_zone": 2, "issn": "2469-9950"},
    "Phys. Rev. Materials": {"full_name": "Physical Review Materials", "if_value": 3.4, "cas_zone": 2, "issn": "2475-9953"},
    "J. Chem. Phys. (JCP)": {"full_name": "The Journal of Chemical Physics", "if_value": 3.1, "cas_zone": 3, "issn": "0021-9606"},
    "Applied Catalysis B: Env.": {"full_name": "Applied Catalysis B: Environmental", "if_value": 22.1, "cas_zone": 1, "issn": "0926-3373"},
    "J. Catalysis": {"full_name": "Journal of Catalysis", "if_value": 7.1, "cas_zone": 1, "issn": "0021-9517"},
    "J. Mater. Chem. A": {"full_name": "Journal of Materials Chemistry A", "if_value": 11.1, "cas_zone": 1, "issn": "2050-7488"},
    "Comput. Mater. Sci.": {"full_name": "Computational Materials Science", "if_value": 3.5, "cas_zone": 2, "issn": "0927-0256"},
    "ChemCatChem": {"full_name": "ChemCatChem", "if_value": 4.5, "cas_zone": 2, "issn": "1867-3880"},
    "npj Comput. Materials": {"full_name": "npj Computational Materials", "if_value": 9.4, "cas_zone": 1, "issn": "2057-3960"},
    "Phys. Chem. Chem. Phys. (PCCP)": {"full_name": "Physical Chemistry Chemical Physics", "if_value": 3.3, "cas_zone": 3, "issn": "1463-9076"},
    "J. Comput. Chem.": {"full_name": "Journal of Computational Chemistry", "if_value": 3.4, "cas_zone": 3, "issn": "0192-8651"},
    "Catalysis Sci. & Tech.": {"full_name": "Catalysis Science & Technology", "if_value": 4.8, "cas_zone": 2, "issn": "2044-4753"},
    "Nature Comput. Sci.": {"full_name": "Nature Computational Science", "if_value": 11.3, "cas_zone": 1, "issn": "2662-8457"},
    "npj Quantum Materials": {"full_name": "npj Quantum Materials", "if_value": 9.9, "cas_zone": 1, "issn": "2056-9351"},
    "Theor. Chem. Acc.": {"full_name": "Theoretical Chemistry Accounts", "if_value": 1.7, "cas_zone": 4, "issn": "1432-881X"},
    "Nature Materials": {"full_name": "Nature Materials", "if_value": 37.2, "cas_zone": 1, "issn": "1476-1122"},
    "Nature": {"full_name": "Nature", "if_value": 50.5, "cas_zone": 1, "issn": "0028-0836"},
    "Science": {"full_name": "Science", "if_value": 44.7, "cas_zone": 1, "issn": "0036-8075"},
    "JACS": {"full_name": "Journal of the American Chemical Society", "if_value": 14.4, "cas_zone": 1, "issn": "0002-7863"},
    "ACS Catalysis": {"full_name": "ACS Catalysis", "if_value": 11.3, "cas_zone": 1, "issn": "2155-5435"},
}

# name 必须与 articles.journal 中的写法一致（RSS/OpenAlex 订阅名）
# if_value: IF(2025)，来自上游 JCR2025；cas_zone 为中科院分区 1-4
SEED_METRICS: list[dict] = [
    {"name": "Angew. Chem. Int. Ed.", "full_name": "Angewandte Chemie International Edition", "if_value": 17.6, "cas_zone": 1, "issn": "1433-7851"},
    {"name": "Nature Chemistry", "full_name": "Nature Chemistry", "if_value": 24.5, "cas_zone": 1, "issn": "1755-4330"},
    {"name": "Chemical Science", "full_name": "Chemical Science", "if_value": 8.1, "cas_zone": 1, "issn": "2041-6520"},
    {"name": "Phys. Rev. Letters (PRL)", "full_name": "Physical Review Letters", "if_value": 9.4, "cas_zone": 1, "issn": "0031-9007"},
    {"name": "Phys. Rev. B", "full_name": "Physical Review B", "if_value": 3.9, "cas_zone": 2, "issn": "2469-9950"},
    {"name": "Phys. Rev. Materials", "full_name": "Physical Review Materials", "if_value": 3.6, "cas_zone": 2, "issn": "2475-9953"},
    {"name": "J. Chem. Phys. (JCP)", "full_name": "The Journal of Chemical Physics", "if_value": 3.7, "cas_zone": 3, "issn": "0021-9606"},
    {"name": "Applied Catalysis B: Env.", "full_name": "Applied Catalysis B: Environmental", "if_value": 19.7, "cas_zone": 1, "issn": "0926-3373"},
    {"name": "J. Catalysis", "full_name": "Journal of Catalysis", "if_value": 6.0, "cas_zone": 1, "issn": "0021-9517"},
    {"name": "J. Mater. Chem. A", "full_name": "Journal of Materials Chemistry A", "if_value": 9.2, "cas_zone": 1, "issn": "2050-7488"},
    {"name": "Comput. Mater. Sci.", "full_name": "Computational Materials Science", "if_value": 3.7, "cas_zone": 2, "issn": "0927-0256"},
    {"name": "ChemCatChem", "full_name": "ChemCatChem", "if_value": 4.1, "cas_zone": 2, "issn": "1867-3880"},
    {"name": "npj Comput. Materials", "full_name": "npj Computational Materials", "if_value": 13.1, "cas_zone": 1, "issn": "2057-3960"},
    {"name": "Phys. Chem. Chem. Phys. (PCCP)", "full_name": "Physical Chemistry Chemical Physics", "if_value": 3.0, "cas_zone": 3, "issn": "1463-9076"},
    {"name": "Chem. Phys. Lett.", "full_name": "Chemical Physics Letters", "if_value": 2.5, "cas_zone": 4, "issn": "0009-2614"},
    {"name": "Machine Learning: Sci. Tech.", "full_name": "Machine Learning: Science and Technology", "if_value": 4.2, "cas_zone": 1, "issn": "2632-2153"},
    {"name": "J. Comput. Chem.", "full_name": "Journal of Computational Chemistry", "if_value": 2.9, "cas_zone": 3, "issn": "0192-8651"},
    {"name": "Catalysis Sci. & Tech.", "full_name": "Catalysis Science & Technology", "if_value": 4.0, "cas_zone": 2, "issn": "2044-4753"},
    {"name": "Nature Comput. Sci.", "full_name": "Nature Computational Science", "if_value": 20.3, "cas_zone": 1, "issn": "2662-8457"},
    {"name": "npj Quantum Materials", "full_name": "npj Quantum Materials", "if_value": 6.6, "cas_zone": 1, "issn": "2397-4648"},
    {"name": "Theor. Chem. Acc.", "full_name": "Theoretical Chemistry Accounts", "if_value": 1.8, "cas_zone": 4, "issn": "1432-881X"},
    {"name": "WIREs Comput. Mol. Sci.", "full_name": "WIREs Computational Molecular Science", "if_value": 10.9, "cas_zone": 1, "issn": "1759-0876"},
    {"name": "Nature Materials", "full_name": "Nature Materials", "if_value": 38.0, "cas_zone": 1, "issn": "1476-1122"},
    {"name": "Nature", "full_name": "Nature", "if_value": 56.1, "cas_zone": 1, "issn": "0028-0836"},
    {"name": "Science", "full_name": "Science", "if_value": 47.3, "cas_zone": 1, "issn": "0036-8075"},
    {"name": "JACS", "full_name": "Journal of the American Chemical Society", "if_value": 16.6, "cas_zone": 1, "issn": "0002-7863"},
    {"name": "ACS Catalysis", "full_name": "ACS Catalysis", "if_value": 13.6, "cas_zone": 1, "issn": "2155-5435"},
    {"name": "ACS Nano", "full_name": "ACS Nano", "if_value": 17.1, "cas_zone": 1, "issn": "1936-0851"},
    {"name": "Nano Letters", "full_name": "Nano Letters", "if_value": 9.6, "cas_zone": 1, "issn": "1530-6984"},
    {"name": "Advanced Materials", "full_name": "Advanced Materials", "if_value": 27.4, "cas_zone": 1, "issn": "0935-9648"},
    {"name": "Energy & Environ. Sci.", "full_name": "Energy & Environmental Science", "if_value": 32.5, "cas_zone": 1, "issn": "1754-5692"},
    {"name": "Chem. Rev.", "full_name": "Chemical Reviews", "if_value": 51.4, "cas_zone": 1, "issn": "0009-2665"},
    {"name": "Chem. Soc. Rev.", "full_name": "Chemical Society Reviews", "if_value": 40.4, "cas_zone": 1, "issn": "0306-0012"},
    # ── 订阅新增：IF 与分区均取自上游同一版本（JCR2025 / FQBJCR2025）──
    {"name": "Nature Communications", "full_name": "Nature Communications", "if_value": 18.1, "cas_zone": 1, "issn": "2041-1723"},
    {"name": "Nature Catalysis", "full_name": "Nature Catalysis", "if_value": 48.3, "cas_zone": 1, "issn": "2520-1158"},
    {"name": "Nature Machine Intelligence（Nature Portfolio）", "full_name": "Nature Machine Intelligence", "if_value": 29.8, "cas_zone": 1, "issn": "2522-5839"},
    {"name": "J. Chem. Theory Comput. (JCTC)", "full_name": "Journal of Chemical Theory and Computation", "if_value": 5.8, "cas_zone": 1, "issn": "1549-9618"},
    {"name": "J. Chem. Inf. Model. (JCIM)", "full_name": "Journal of Chemical Information and Modeling", "if_value": 6.4, "cas_zone": 2, "issn": "1549-9596"},
    {"name": "J. Phys. Chem. A", "full_name": "Journal of Physical Chemistry A", "if_value": 3.0, "cas_zone": 2, "issn": "1089-5639"},
    {"name": "J. Phys. Chem. B", "full_name": "Journal of Physical Chemistry B", "if_value": 3.2, "cas_zone": 2, "issn": "1520-6106"},
    {"name": "J. Phys. Chem. C", "full_name": "Journal of Physical Chemistry C", "if_value": 3.4, "cas_zone": 3, "issn": "1932-7447"},
    {"name": "J. Phys. Chem. Letters (JPCL)", "full_name": "Journal of Physical Chemistry Letters", "if_value": 4.5, "cas_zone": 2, "issn": "1948-7185"},
    {"name": "ACS Applied Materials & Interfaces", "full_name": "ACS Applied Materials & Interfaces", "if_value": 7.8, "cas_zone": 2, "issn": "1944-8244"},
    {"name": "Chemistry of Materials", "full_name": "Chemistry of Materials", "if_value": 7.1, "cas_zone": 2, "issn": "0897-4756"},
    {"name": "Physical Review Applied", "full_name": "Physical Review Applied", "if_value": 4.4, "cas_zone": 2, "issn": "2331-7019"},
    {"name": "Physical Review Research", "full_name": "Physical Review Research", "if_value": 4.0, "cas_zone": 2, "issn": "2643-1564"},
    {"name": "Digital Discovery", "full_name": "Digital Discovery", "if_value": 7.1, "cas_zone": 2, "issn": "2635-098X"},
    # MGE advance：上游有 IF，但该版本没有中科院分区与预警标记，不补分区
    {"name": "MGE advance", "full_name": "Materials Genome Engineering Advances", "if_value": 13.9, "issn": "2940-9489"},
]


def zone_label(zone) -> str:
    if zone in (1, "1"):
        return "1区"
    if zone in (2, "2"):
        return "2区"
    if zone in (3, "3"):
        return "3区"
    if zone in (4, "4"):
        return "4区"
    return ""
