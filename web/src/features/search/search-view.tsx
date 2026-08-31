import { useState } from "react";
import { IconSearch } from "@tabler/icons-react";

import type { Answer } from "@/api/contracts";
import { api } from "@/api/client";
import { Button, Notice, PageHeading } from "@/components/ui";
import { useI18n } from "@/lib/i18n";

export function SearchView(): React.ReactElement {
  const { t } = useI18n();
  const [question, setQuestion] = useState(""); const [answer, setAnswer] = useState<Answer | null>(null); const [error, setError] = useState<string | null>(null); const [working, setWorking] = useState(false);
  const ask = async (event: React.FormEvent): Promise<void> => { event.preventDefault(); if (!question.trim()) return; setWorking(true); setError(null); try { setAnswer(await api.ask(question.trim())); } catch (reason) { setError(reason instanceof Error ? reason.message : t("search.failed")); } finally { setWorking(false); } };
  return <div className="page-stack"><PageHeading title={t("page.search.title")} copy={t("page.search.copy")} />{error ? <Notice tone="error" onDismiss={() => setError(null)}>{error}</Notice> : null}<form className="question-form" onSubmit={(event) => void ask(event)}><label htmlFor="question">{t("search.question")}</label><div><input id="question" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={t("search.placeholder")} /><Button className="button-primary" type="submit" disabled={working}><IconSearch size={18} />{working ? t("action.searching") : t("search.ask")}</Button></div></form>{answer ? <section className="answer-card"><span className="eyebrow">{t("search.mode", { mode: answer.mode })}</span><p>{answer.text}</p>{answer.sql_result ? <pre>{JSON.stringify(answer.sql_result, null, 2)}</pre> : null}{answer.citations.length ? <div className="citation-list">{answer.citations.map((citation) => <article key={`${citation.document_id}-${citation.heading_path}`}><strong>{citation.heading_path}</strong><p>{citation.snippet}</p><small>{citation.document_id}</small></article>)}</div> : null}</section> : null}</div>;
}

export default SearchView;
