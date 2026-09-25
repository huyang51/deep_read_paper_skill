from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional


class ResponseFormat(str, Enum):
    json = "json"
    markdown = "markdown"


class SearchInput(BaseModel):
    query: str = Field(description="搜索查询文本（中英文均可）")
    n_results: int = Field(default=5, description="返回结果数量")
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class GetPaperInput(BaseModel):
    paper_id: int = Field(description="论文ID")


class FindRelatedInput(BaseModel):
    paper_id: int = Field(description="论文ID")
    relation_type: Optional[str] = Field(default=None, description="关系类型过滤: method_similar, problem_related, complementary, evolutionary")


class SearchByMethodInput(BaseModel):
    method_category: str = Field(description="方法类别，如 Contrastive Learning, Diffusion, Transformer")


class PaperIndexInput(BaseModel):
    paper_id: Optional[int] = Field(default=None, description="论文ID（新建时留空，自动分配）")
    title: str = Field(description="论文标题")
    short_name: str = Field(default="", description="模型/方法简称（如 ReT, PreFLMR），用于文件名和图谱节点名")
    year: int = Field(description="发表年份")
    venue: str = Field(default="", description="会议/期刊")
    authors: list[str] = Field(default_factory=list)
    method_category: str = Field(default="", description="方法类别")
    problem_domain: str = Field(default="", description="问题领域")
    keywords: list[str] = Field(default_factory=list)
    core_contribution: str = Field(default="", description="一句话核心贡献")
    novelty_level: str = Field(default="", description="新颖性定级: incremental | substantial | breakthrough")
    related_papers: list[int] = Field(default_factory=list)
    date_read: str = Field(default="", description="阅读日期 YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$|^$")
    read_mode: str = Field(default="standard", pattern=r"^(quick|standard|deep)$", description="Phase 0 分诊档位")
    aliases: list[str] = Field(default_factory=list, description="别名列表（用于 Obsidian 图谱显示和搜索）")
    tags: list[str] = Field(default_factory=list)
    body: str = Field(default="", description="论文结构化摘要 body（Markdown）")


class PaperRemoveInput(BaseModel):
    paper_id: int = Field(description="要删除的论文ID")


class CiteVerifyInput(BaseModel):
    query: str = Field(default="", description="被断言论文的标题（或 OpenAlex ID）")
    author: str = Field(default="", description="声称的代表作者（可选交叉核对）")
    year: Optional[int] = Field(default=None, description="声称的发表年份（±1 容忍）")
    doi: str = Field(default="", description="DOI（优先精确解析）")
    arxiv_id: str = Field(default="", description="arXiv ID，如 1706.03762")


class PaperCitationsInput(BaseModel):
    query: str = Field(default="", description="论文标题（无 ID 时用）")
    doi: str = Field(default="")
    arxiv_id: str = Field(default="")
    openalex_id: str = Field(default="")
    n_citing: int = Field(default=15, ge=1, le=50, description="Top 引用工作数量")
    include_references: bool = Field(default=True, description="是否返回其参考文献列表")
