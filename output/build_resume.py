from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
d=Document(); s=d.sections[0]; s.page_width=Cm(21); s.page_height=Cm(29.7)
s.top_margin=Cm(1.35); s.bottom_margin=Cm(1.35); s.left_margin=Cm(1.65); s.right_margin=Cm(1.65)
for name in ['Normal','Title','Heading 1','Heading 2']:
 st=d.styles[name]; st.font.name='Calibri'; st.font.color.rgb=RGBColor(0,0,0); st._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'微软雅黑')
st=d.styles['Normal']; st.font.size=Pt(10); st.paragraph_format.line_spacing=1.12; st.paragraph_format.space_after=Pt(3)
def p(text,bold=False,size=None,style=None):
 x=d.add_paragraph(style=style); r=x.add_run(text); r.bold=bold
 if size: r.font.size=Pt(size)
 return x
def h(t):
 x=p(t,True,12,'Heading 1'); x.paragraph_format.space_before=Pt(9); x.paragraph_format.space_after=Pt(4)
def bullet(label,t):
 x=d.add_paragraph(); x.paragraph_format.left_indent=Cm(.22); x.paragraph_format.first_line_indent=Cm(-.22)
 x.add_run('• '+label+'：').bold=True; x.add_run(t)
p('刘宗珅',True,23,'Title').paragraph_format.space_after=Pt(3)
p('求职意向  大模型应用开发实习生 / Agent 开发实习生',True,10.5)
p('18263919367  |  zshade@qq.com',size=10)
h('教育背景')
p('中国科学院大学 · 信息工程研究所  |  网络与信息安全  |  硕士在读',True)
p('山东大学  |  计算机科学与技术  |  本科                                      2022—2026',True)
p('本科成绩：89.13/100  |  专业排名：11/28  |  CET-6：463 分')
p('荣誉：2023 年优秀学生一等奖学金、校级三好学生；2025 年优秀学生三等奖学金。')
h('项目经历')
p('RAG 企业知识库',True,11)
p('Python / FastAPI / LangGraph / Milvus / BGE / PostgreSQL / MinIO / Docker Compose',size=9)
p('构建文档上传、解析、索引与跨文档问答流程，支持来源引用、检索纠错和异步导入。')
bullet('混合检索','保留 Markdown 标题层级与来源位置，结合父子章节合并、语义分块组织文本；融合稠密向量与 BM25 检索，使用 RRF 和 BGE 重排序筛选回答证据。')
bullet('问答编排','基于 LangGraph 实现复合问题拆分、多查询检索和相关性判断；加入答案依据检查、有限重试及证据不足拒答，支持带来源引用的跨文档回答。')
bullet('任务可靠性','基于 PostgreSQL 构建持久化任务队列，通过失败重试、租约栅栏和断点式导入处理任务中断；按知识库、文档、版本隔离检索，避免旧快照与不同向量模型混用。')
bullet('工程验证','接入前端任务状态展示，提供语料审计、回归评估与旧快照清理工具；已有验收记录中，默认测试 45 项通过、9 项跳过，真实 PostgreSQL 集成测试 7 项通过。')
x=p('代码审查 Agent',True,11); x.paragraph_format.space_before=Pt(6)
p('Python / LangGraph / ReAct / AST / BM25 / REST API / SSE / MCP',size=9)
p('构建覆盖静态分析、上下文取证、人工确认与修复验证的 Python 代码审查工作流。')
bullet('多 Agent 编排','基于 LangGraph 构建主助手与安全、缺陷、规范、性能、修复 5 类子助手，按职责配置工具；通过状态栈和检查点实现任务交接与中断恢复。')
bullet('证据校验','集成静态分析、AST 和代码库 BM25 检索，校验结论证据、工具调用记录与代码行号，过滤无依据结论；分离只读与敏感操作，并在敏感操作前引入人工确认。')
bullet('执行与修复','实现 ReAct 运行时，限制执行步数、工具调用次数、Token 用量和耗时；修复代码依次通过语法、接口兼容和受限子进程行为验证，失败时重试或回退原代码。')
bullet('接入与评估','提供 REST API、SSE、MCP Server 和 VS Code 插件入口，记录工具调用、耗时与 Token 轨迹；支持单 Agent、多 Agent 及静态规则基线对比评估。')
h('专业技能')
bullet('编程基础','熟悉 Python，具备 C、Java 编程基础；掌握数据结构、计算机网络、操作系统等基础知识。')
bullet('大模型应用','熟悉 LangChain、LangGraph、ReAct 与工具调用；具备 RAG、多 Agent 编排、结构化输出校验及人工确认流程开发经验。')
bullet('后端工程','具备 FastAPI 接口开发，PostgreSQL、Milvus、MinIO 数据管理，以及 Git、Docker Compose 部署和接口联调经验。')
d.core_properties.author='刘宗珅'; d.core_properties.title='刘宗珅 大模型应用开发实习简历'
d.save(r'C:/Projects/code_review/output/刘宗珅_大模型应用开发实习简历_优化版.docx')
