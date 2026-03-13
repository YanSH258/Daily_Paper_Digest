"""
stat_db.py - 数据库文献统计与分析工具
用于分析 chem_daily.db 中已抓取的历史文献分布情况
支持统计大盘展示、关键字搜索以及导出为 CSV (Excel)

用法：
  python src/utils/stat_db.py
  python src/utils/stat_db.py --search "关键字" --export my_papers.csv
"""
import sqlite3
import argparse
import csv
import sys
from pathlib import Path
from collections import Counter

# 将 src/ 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.notifier import classify_article

def main():
    parser = argparse.ArgumentParser(description="统计和搜索数据库中的文献")
    parser.add_argument("--db", default="data/db/chem_daily.db", help="数据库文件路径")
    parser.add_argument("--threshold", type=int, default=5, help="高相关性分数的阈值")
    parser.add_argument("--search", type=str, default="", help="搜索关键字（匹配标题/摘要/期刊/作者）")
    parser.add_argument("--export", type=str, default="", help="导出为 CSV 文件的路径（例如：data.csv）")
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        
        # 构建查询语句
        query = "SELECT * FROM articles"
        params = []
        if args.search:
            # 在标题、摘要、作者和期刊中模糊搜索
            query += " WHERE title LIKE ? OR abstract LIKE ? OR authors LIKE ? OR journal LIKE ?"
            like_term = f"%{args.search}%"
            params = [like_term, like_term, like_term, like_term]
            
        articles = conn.execute(query, params).fetchall()
    except Exception as e:
        print(f"\n❌ 无法读取数据库 {args.db}: {e}\n")
        return

    total_articles = len(articles)
    if total_articles == 0:
        if args.search:
            print(f"\n📂 未找到包含关键字 '{args.search}' 的文献。\n")
        else:
            print(f"\n📂 数据库 {args.db} 目前为空。\n")
        return

    # ── 1. 导出为 CSV 逻辑 ──
    if args.export:
        try:
            # 使用 utf-8-sig 防止 Excel 打开中文乱码
            with open(args.export, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(["ID", "DOI", "期刊", "研究方向", "AI评分", "发表日期", "标题", "作者", "链接"])
                for row in articles:
                    article = dict(row)
                    topic = classify_article(article)
                    writer.writerow([
                        article.get('id'), 
                        article.get('doi'), 
                        article.get('journal'), 
                        topic, 
                        article.get('relevance'), 
                        article.get('pub_date'),
                        article.get('title'), 
                        article.get('authors'), 
                        article.get('url')
                    ])
            print(f"\n✅ 成功导出 {total_articles} 篇文章到文件: {args.export}")
            print(f"💡 提示: 你可以直接用 Excel 打开 {args.export} 进行查看和筛选。\n")
        except Exception as e:
            print(f"\n❌ 导出 CSV 失败: {e}\n")
        
        # 如果仅仅是导出，可以直接结束（除非也指定了 search，那就顺便打印一下结果）
        if not args.search:
            return

    # ── 2. 搜索结果展示 ──
    if args.search:
        print(f"\n🔍 搜索关键字: '{args.search}' (共找到 {total_articles} 篇)")
        print("-" * 80)
        # 最多在终端打印前 15 条，避免刷屏
        for i, row in enumerate(articles[:15], 1):
            article = dict(row)
            score = article.get('relevance', 0)
            topic = classify_article(article)
            title = article.get('title', '')[:70] + ("..." if len(article.get('title', '')) > 70 else "")
            
            print(f"[{i}] 分数:{score:.1f} | {article.get('journal')} | [{topic}]")
            print(f"    📄 {title}")
            print(f"    🔗 {article.get('url')}\n")
            
        if total_articles > 15:
            print(f"... 等共 {total_articles} 篇。")
            if not args.export:
                print(f"💡 提示: 结果太多？你可以使用 `python stat_db.py --search \"{args.search}\" --export result.csv` 导出为 Excel 查看全部。")
        print("=" * 80 + "\n")
        return

    # ── 3. 统计大盘 (Dashboard) ──
    # 初始化统计变量
    topics_counter = Counter()
    journal_counter = Counter()
    high_rel_topics = Counter()
    high_rel_count = 0
    valid_dates = []

    for row in articles:
        article = dict(row)
        
        topic = classify_article(article)
        topics_counter[topic] += 1
        journal_counter[article.get('journal', 'Unknown')] += 1
        
        score = article.get('relevance', 0)
        if score >= args.threshold:
            high_rel_count += 1
            high_rel_topics[topic] += 1
            
        pub_date = article.get('pub_date')
        if pub_date and len(pub_date) >= 10:
            valid_dates.append(pub_date[:10])

    # 打印精美统计面板
    print("\n" + "="*60)
    print(" 📊 个人文献数据库统计大盘 (Database Dashboard) ")
    print("="*60)
    print(f" 📚 总计收录文章 : {total_articles} 篇")
    print(f" ⭐ 高相关性文章 : {high_rel_count} 篇 (AI评分 >= {args.threshold})")
    
    if valid_dates:
        earliest = min(valid_dates)
        latest = max(valid_dates)
        print(f" 📅 收录时间范围 : {earliest} 至 {latest}")
    print("-" * 60)
    
    print("\n 🔍 【按研究方向分布】(包含所有文章):")
    for topic, count in topics_counter.most_common():
        high_count = high_rel_topics[topic]
        percentage = (count / total_articles) * 100
        print(f"  • {topic: <20} | 总计: {count:>4} 篇 ({percentage:>5.1f}%) | 高相关: {high_count:>3} 篇")

    print("\n" + "-" * 60)
    print(" 📓 【各大期刊收录排行榜】(Top 10):")
    for journal, count in journal_counter.most_common(10):
        print(f"  • {journal: <25} | {count:>4} 篇")
        
    print("="*60 + "\n")
    print("💡 提示: 新增导出功能！尝试运行 `python stat_db.py --export my_papers.csv` 导出所有文章为 Excel。\n")

if __name__ == "__main__":
    main()