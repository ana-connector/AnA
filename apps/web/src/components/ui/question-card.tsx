"use client";

import { useState, type FormEvent } from "react";
import { MessageCircleQuestionMarkIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Questionnaire,
  QuestionnaireActions,
  QuestionnaireChoice,
  QuestionnaireChoices,
  QuestionnaireDescription,
  QuestionnaireError,
  QuestionnaireItem,
  QuestionnaireNext,
  QuestionnairePrevious,
  QuestionnaireProgress,
  QuestionnaireSubmit,
  QuestionnaireTitle,
} from "@/components/ui/questionnaire";
import type { QuestionRequest } from "@/hooks/useChatSocket";

/** The agent's own question, in the transcript where the conversation is
 *  blocked on it — next to the approval cards, because it is the same kind of
 *  thing: the turn is parked until a human answers.
 *
 *  One item per question, with only the choices the agent supplied. */
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
      return question.multi ? picked : picked[0] ?? "";
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
      </div>

      <Questionnaire items={items} onSubmit={submit}>
        {request.questions.length > 1 && <QuestionnaireProgress />}
        {request.questions.map((question, index) => (
          <QuestionnaireItem key={index} name={`q${index}`} multiple={question.multi} required>
            <QuestionnaireTitle>{question.question}</QuestionnaireTitle>
            {question.note ? (
              <QuestionnaireDescription>{question.note}</QuestionnaireDescription>
            ) : null}

            <QuestionnaireChoices>
              {question.options.map((option) => (
                <QuestionnaireChoice key={option} value={option}>
                  {option}
                </QuestionnaireChoice>
              ))}
            </QuestionnaireChoices>
            <QuestionnaireError />
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
