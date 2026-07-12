from tool_registry import tool

_fmt = lambda x, y: f"{x} + {y}"  # 辅助 lambda（不会被暴露为工具）

@tool
def create_math_pdf(
    age: int,
    score: int,
    difficulty_level: int,
    problems: list,
    output_path: str = "math_exercises.pdf"
) -> str:
    """
    生成一张包含数学练习题的精美 PDF，适合小朋友打印使用。

    什么时候使用：
    - 用户（家长/老师）确认题目合适后，调用此工具生成 PDF
    - 将 HITL 审批通过的题目生成可打印的 PDF 文件

    参数:
        age: 学生年龄（7-15岁）
        score: 数学成绩（百分制，0-100）
        difficulty_level: 难度等级（1=简单 2=中等 3=困难）
        problems: 题目列表，每项为 {"question": str, "answer": str, "points": int}
        output_path: 输出 PDF 文件路径（默认 math_exercises.pdf）

    返回:
        PDF 文件的绝对路径
    """
    try:
        from fpdf import FPDF
    except ImportError:
        raise ImportError(
            "❌ 需要安装 fpdf2 库才能生成 PDF。\n"
            "   请在终端运行: pip install fpdf2"
        )

    # --- 根据年级和难度确定标题与说明 ---
    grade_map = {7: "一年级", 8: "二年级", 9: "三年级", 10: "四年级",
                 11: "五年级", 12: "六年级", 13: "初一", 14: "初二", 15: "初三"}
    grade = grade_map.get(age, f"第{age-6}年级")

    diff_labels = {1: "⭐ 基础练习", 2: "⭐⭐ 进阶训练", 3: "⭐⭐⭐ 挑战题"}
    diff_label = diff_labels.get(difficulty_level, "数学练习")

    # --- 创建 PDF ---
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()

    # 字体设置（需要中文字体，使用系统常见的黑体/宋体）
    # fpdf2 默认不支持中文，这里用 add_font 加载一个常见中文字体
    # 如果系统没有这些字体，用户可以安装或改用 DejaVu（但 DejaVu 不含中文）
    # 方案：尝试加载几个常见中文字体路径
    font_paths = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",   # Linux 文泉驿
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/simhei.ttf",                     # Windows 黑体
        "C:/Windows/Fonts/simsun.ttc",                     # Windows 宋体
        "/System/Library/Fonts/PingFang.ttc",              # macOS 苹方
    ]
    font_loaded = False
    for fp in font_paths:
        try:
            pdf.add_font("CJK", "", fp, uni=True)
            pdf.set_font("CJK", "", 16)
            font_loaded = True
            break
        except RuntimeError:
            continue

    if not font_loaded:
        # 回退：用内置的 Courier（仅英文数字可用）
        pdf.set_font("Courier", "", 14)
        fallback_note = (
            "NOTE: No CJK font found. Install wqy-zenhei on Linux,\n"
            "or use a font that supports Chinese characters.\n"
        )
    else:
        fallback_note = ""

    # --- 页眉 ---
    pdf.set_fill_color(230, 240, 255)  # 浅蓝色背景
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_text_color(30, 60, 120)
    if font_loaded:
        pdf.set_font("CJK", "", 20)
        pdf.cell(0, 12, f"🧮 小学数学练习", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("CJK", "", 12)
        pdf.cell(0, 8, f"{grade}  |  难度: {diff_label}  |  成绩参考: {score}分",
                 align="C", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 10, "MATH EXERCISES", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 8, f"Grade: {grade}  Difficulty: {difficulty_level}",
                 align="C", new_x="LMARGIN", new_y="NEXT")

    if fallback_note:
        pdf.set_font("Courier", "", 9)
        pdf.set_text_color(200, 50, 50)
        pdf.multi_cell(0, 5, fallback_note)

    # --- 分割线 ---
    pdf.set_draw_color(100, 150, 200)
    pdf.line(10, 35, 200, 35)
    pdf.ln(8)

    # --- 题目列表 ---
    pdf.set_text_color(0, 0, 0)
    if font_loaded:
        pdf.set_font("CJK", "", 14)
    else:
        pdf.set_font("Courier", "", 12)

    total_points = 0
    for i, prob in enumerate(problems, 1):
        q = prob.get("question", "")
        a = prob.get("answer", "")
        pts = prob.get("points", 5)
        total_points += pts

        # 题号 + 题目
        if font_loaded:
            pdf.set_font("CJK", "", 13)
            pdf.cell(0, 8, f"第{i}题（{pts}分）", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("CJK", "", 12)
            pdf.multi_cell(0, 7, q)
        else:
            pdf.cell(0, 8, f"#{i} ({pts}pts)", new_x="LMARGIN", new_y="NEXT")
            pdf.multi_cell(0, 7, q)

        # 留空作答区域
        pdf.ln(4)

    # --- 总分 ---
    pdf.ln(6)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    if font_loaded:
        pdf.set_font("CJK", "", 13)
        pdf.cell(0, 8, f"📊 总分: {total_points}分    姓名: ________    日期: ________",
                 align="C", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 8, f"Total: {total_points}pts   Name: ________   Date: ________",
                 align="C", new_x="LMARGIN", new_y="NEXT")

    # --- 页脚：参考答案（小字，在末尾）---
    pdf.ln(10)
    pdf.set_text_color(120, 120, 120)
    if font_loaded:
        pdf.set_font("CJK", "", 10)
        pdf.cell(0, 6, "📝 参考答案（家长用）", align="C", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Courier", "", 9)
        pdf.cell(0, 6, "ANSWER KEY (for parents)", align="C", new_x="LMARGIN", new_y="NEXT")

    for i, prob in enumerate(problems, 1):
        a = prob.get("answer", "")
        if font_loaded:
            pdf.set_font("CJK", "", 9)
            pdf.cell(0, 5, f"  第{i}题: {a}", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.cell(0, 5, f"  #{i}: {a}", new_x="LMARGIN", new_y="NEXT")

    # --- 输出 ---
    pdf.output(output_path)
    import os
    abs_path = os.path.abspath(output_path)
    return f"✅ PDF 已生成: {abs_path}"
