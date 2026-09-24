"use client";

import { useState, type FormEvent } from "react";
import { MessageCircleQuestionMarkIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Questionnaire,
  QuestionnaireActions,
  QuestionnaireChoice,
  QuestionnaireChoiceDescription,
  QuestionnaireChoices,
  QuestionnaireDescription,
  QuestionnaireInput,
  QuestionnaireItem,
  QuestionnaireNext,
  QuestionnairePrevious,
  QuestionnaireProgress,
  QuestionnaireSubmit,
  QuestionnaireTitle,
} from "@/components/ui/questionnaire";
import { Input } from "@/components/ui/input";
import type { QuestionRequest } from "@/hooks/useChatSocket";

/** The agent's own question, in the transcript where the conversation is
 *  blocked on it — next to the approval cards, because it is the same kind of
 *  thing: the turn is parked until a human answers.
 *
 *  One item per question, so several questions are a short sequence rather than
 *  a wall of fields. A question with options offers them (radio, or checkboxes
 *  when the agent said several may be picked) and always keeps a free-text box,
 *  since the option the agent thought of is not always the answer. */
export function QuestionCard({
  request,
  onAnswer,
}: {
  request: QuestionRequest;
  onAnswer: (requestId: string, answers: (string | string[])[]) => void;
}) {
  const [submitted, setSubmitted] = useState(false);
  const items = request.questions.map((_, index) => ({ name: `q${index}` }));

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitted) return;
    const data = new FormData(event.currentTarget);
    const answers = request.questions.map((question, index) => {
      const picked = data.getAll(`q${index}`).map(String).filter((value) => value.length > 0);
      const typed = String(data.get(`q${index}-other`) ?? "").trim();
      // The typed answer is added rather than preferred: someone who ticks an
      // option *and* explains themselves means both.
      const all = typed ? [...picked, typed] : picked;
      return question.multi ? all : all[0] ?? "";
    });
    setSubmitted(true);
    onAnswer(request.request_id, answers);
  };

  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-card p-3">
      <div className="flex items-center gap-2">
        <MessageCircleQuestionMarkIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="text-sm font-medium">확인이 필요합니다</span>
        <Badge variant="outline">{request.agent ?? request.role}</Badge>
        {request.questions.length > 1 && (
          <QuestionnaireProgress className="ml-auto" render={<span />} />
        )}
      </div>

      <Questionnaire items={items} onSubmit={submit}>
        {request.questions.map((question, index) => (
          <QuestionnaireItem key={index} name={`q${index}`} multiple={question.multi}>
            <QuestionnaireTitle>{question.question}</QuestionnaireTitle>
            {question.note ? (
              <QuestionnaireDescription>{question.note}</QuestionnaireDescription>
            ) : null}

            {question.options.length > 0 && (
              <QuestionnaireChoices>
                {question.options.map((option) => (
                  <QuestionnaireChoice key={option} value={option}>
                    {option}
                  </QuestionnaireChoice>
                ))}
              </QuestionnaireChoices>
            )}

            {question.options.length === 0 ? (
              // No options: the item itself is the answer, so the primitive's
              // own input carries the item's name and its required check.
              <QuestionnaireInput placeholder="답을 적으세요" required />
            ) : (
              // With options, the primitive's input would take the *same* name
              // as the choices, so the escape hatch is a plain field of its own.
              // It is always offered: the options are what the agent thought of,
              // and the answer it did not think of is the one worth having.
              <Input name={`q${index}-other`} placeholder="다른 답 (선택)" />
            )}
            {question.options.length > 0 && question.multi && (
              <QuestionnaireChoiceDescription className="text-xs">
                여러 개를 고를 수 있습니다.
              </QuestionnaireChoiceDescription>
            )}
          </QuestionnaireItem>
        ))}

        <QuestionnaireActions>
          {request.questions.length > 1 ? <QuestionnairePrevious size="sm">이전</QuestionnairePrevious> : null}
          {request.questions.length > 1 ? <QuestionnaireNext size="sm">다음</QuestionnaireNext> : null}
          <QuestionnaireSubmit size="sm" disabled={submitted}>
            {submitted ? "전달 중…" : "답하기"}
          </QuestionnaireSubmit>
        </QuestionnaireActions>
      </Questionnaire>
    </div>
  );
}
