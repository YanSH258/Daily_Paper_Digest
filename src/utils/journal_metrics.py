"""本地期刊指标：JCR 影响因子 + 中科院分区（参考值，可手工/脚本更新）。"""

# name 必须与 articles.journal 中的写法一致（RSS 短名）
# if_value: JCR 影响因子（近年公开参考值，非官方实时 API）
# cas_zone: 中科院分区 1-4（升级版，近年常见划分）
SEED_METRICS: list[dict] = [
    {"name": "Angew. Chem. Int. Ed.", "full_name": "Angewandte Chemie International Edition", "if_value": 16.6, "cas_zone": 1, "issn": "1433-7851"},
    {"name": "Nature Chemistry", "full_name": "Nature Chemistry", "if_value": 23.2, "cas_zone": 1, "issn": "1755-4330"},
    {"name": "Chemical Science", "full_name": "Chemical Science", "if_value": 7.6, "cas_zone": 1, "issn": "2041-6520"},
    {"name": "Phys. Rev. Letters (PRL)", "full_name": "Physical Review Letters", "if_value": 8.6, "cas_zone": 1, "issn": "0031-9007"},
    {"name": "Phys. Rev. B", "full_name": "Physical Review B", "if_value": 3.7, "cas_zone": 2, "issn": "2469-9950"},
    {"name": "Phys. Rev. Materials", "full_name": "Physical Review Materials", "if_value": 3.4, "cas_zone": 2, "issn": "2475-9953"},
    {"name": "J. Chem. Phys. (JCP)", "full_name": "The Journal of Chemical Physics", "if_value": 3.1, "cas_zone": 3, "issn": "0021-9606"},
    {"name": "Applied Catalysis B: Env.", "full_name": "Applied Catalysis B: Environmental", "if_value": 22.1, "cas_zone": 1, "issn": "0926-3373"},
    {"name": "J. Catalysis", "full_name": "Journal of Catalysis", "if_value": 7.1, "cas_zone": 1, "issn": "0021-9517"},
    {"name": "J. Mater. Chem. A", "full_name": "Journal of Materials Chemistry A", "if_value": 11.1, "cas_zone": 1, "issn": "2050-7488"},
    {"name": "Comput. Mater. Sci.", "full_name": "Computational Materials Science", "if_value": 3.5, "cas_zone": 2, "issn": "0927-0256"},
    {"name": "ChemCatChem", "full_name": "ChemCatChem", "if_value": 4.5, "cas_zone": 2, "issn": "1867-3880"},
    {"name": "npj Comput. Materials", "full_name": "npj Computational Materials", "if_value": 9.4, "cas_zone": 1, "issn": "2057-3960"},
    {"name": "Phys. Chem. Chem. Phys. (PCCP)", "full_name": "Physical Chemistry Chemical Physics", "if_value": 3.3, "cas_zone": 3, "issn": "1463-9076"},
    {"name": "Chem. Phys. Lett.", "full_name": "Chemical Physics Letters", "if_value": 2.5, "cas_zone": 4, "issn": "0009-2614"},
    {"name": "Machine Learning: Sci. Tech.", "full_name": "Machine Learning: Science and Technology", "if_value": 4.2, "cas_zone": 1, "issn": "2632-2153"},
    {"name": "J. Comput. Chem.", "full_name": "Journal of Computational Chemistry", "if_value": 3.4, "cas_zone": 3, "issn": "0192-8651"},
    {"name": "Catalysis Sci. & Tech.", "full_name": "Catalysis Science & Technology", "if_value": 4.8, "cas_zone": 2, "issn": "2044-4753"},
    {"name": "Nature Comput. Sci.", "full_name": "Nature Computational Science", "if_value": 11.3, "cas_zone": 1, "issn": "2662-8457"},
    {"name": "npj Quantum Materials", "full_name": "npj Quantum Materials", "if_value": 9.9, "cas_zone": 1, "issn": "2056-9351"},
    {"name": "Theor. Chem. Acc.", "full_name": "Theoretical Chemistry Accounts", "if_value": 1.7, "cas_zone": 4, "issn": "1432-881X"},
    {"name": "WIREs Comput. Mol. Sci.", "full_name": "WIREs Computational Molecular Science", "if_value": 10.9, "cas_zone": 1, "issn": "1759-0876"},
    {"name": "Nature Materials", "full_name": "Nature Materials", "if_value": 37.2, "cas_zone": 1, "issn": "1476-1122"},
    {"name": "Nature", "full_name": "Nature", "if_value": 50.5, "cas_zone": 1, "issn": "0028-0836"},
    {"name": "Science", "full_name": "Science", "if_value": 44.7, "cas_zone": 1, "issn": "0036-8075"},
    {"name": "JACS", "full_name": "Journal of the American Chemical Society", "if_value": 14.4, "cas_zone": 1, "issn": "0002-7863"},
    {"name": "ACS Catalysis", "full_name": "ACS Catalysis", "if_value": 11.3, "cas_zone": 1, "issn": "2155-5435"},
    {"name": "ACS Nano", "full_name": "ACS Nano", "if_value": 17.1, "cas_zone": 1, "issn": "1936-0851"},
    {"name": "Nano Letters", "full_name": "Nano Letters", "if_value": 9.6, "cas_zone": 1, "issn": "1530-6984"},
    {"name": "Advanced Materials", "full_name": "Advanced Materials", "if_value": 27.4, "cas_zone": 1, "issn": "0935-9648"},
    {"name": "Energy & Environ. Sci.", "full_name": "Energy & Environmental Science", "if_value": 32.5, "cas_zone": 1, "issn": "1754-5692"},
    {"name": "Chem. Rev.", "full_name": "Chemical Reviews", "if_value": 51.4, "cas_zone": 1, "issn": "0009-2665"},
    {"name": "Chem. Soc. Rev.", "full_name": "Chemical Society Reviews", "if_value": 40.4, "cas_zone": 1, "issn": "0306-0012"},
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
