"use client";

import { useState, type FormEvent } from "react";
import { MessageCircleQuestionMarkIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Questionnaire,
  QuestionnaireActions,
  QuestionnaireChoice,
  QuestionnaireChoices,
  QuestionnaireDescription,
  QuestionnaireError,
  QuestionnaireInput,
  QuestionnaireItem,
  QuestionnaireNext,
  QuestionnairePrevious,
  QuestionnaireProgress,
  QuestionnaireSkip,
  QuestionnaireSubmit,
  QuestionnaireTitle,
} from "@/components/ui/questionnaire";
import type { QuestionRequest } from "@/hooks/useChatSocket";

/** The agent's questions stay in one card while Questionnaire manages steps,
 * selection, validation, shortcuts and optional skips. */
export function QuestionCard({
  request,
  onAnswer,
}: {
  request: QuestionRequest;
  onAnswer: (requestId: string, answers: (string | string[])[]) => boolean;
}) {
  const [submitted, setSubmitted] = useState(false);
  const items = request.questions.map((question, index) => ({
    name: `q${index}`,
    required: question.required !== false,
  }));

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitted) return;
    const data = new FormData(event.currentTarget);
    const answers = request.questions.map((question, index) => {
      const picked = data.getAll(`q${index}`).map(String).filter((value) => value.trim().length > 0);
      return question.multi ? picked : picked[0] ?? "";
    });
    if (onAnswer(request.request_id, answers)) setSubmitted(true);
  };

  return (
    <Questionnaire
      className="mx-auto w-full max-w-xl"
      defaultItem="q0"
      items={items}
      shortcuts="numbers"
      onSubmit={submit}
    >
      <Card>
        {request.questions.map((question, index) => (
          <QuestionnaireItem
            key={index}
            name={`q${index}`}
            multiple={question.multi}
            required={question.required !== false}
          >
            <CardHeader>
              <QuestionnaireTitle render={<CardTitle />}>
                <MessageCircleQuestionMarkIcon className="mr-2 inline size-4 text-muted-foreground" />
                {question.question}
              </QuestionnaireTitle>
              {question.note && (
                <QuestionnaireDescription render={<CardDescription />}>
                  {question.note}
                </QuestionnaireDescription>
              )}
              <CardAction className="flex items-center gap-2">
                <Badge variant="outline">{request.agent ?? request.role}</Badge>
                {request.questions.length > 1 && <QuestionnaireProgress />}
              </CardAction>
            </CardHeader>
            <CardContent>
              <QuestionnaireChoices>
                {question.options.map((option) => {
                  const value = typeof option === "string" ? option : option.value;
                  const label = typeof option === "string" ? option : option.label;
                  const description = typeof option === "string" ? "" : option.description;
                  return (
                    <QuestionnaireChoice key={value} value={value}>
                      <span className="font-medium">{label}</span>
                      {description && <span className="text-muted-foreground">{description}</span>}
                    </QuestionnaireChoice>
                  );
                })}
                {question.input && (
                  <QuestionnaireInput
                    aria-label={question.input.label}
                    placeholder={question.input.placeholder ?? question.input.label}
                  />
                )}
              </QuestionnaireChoices>
              <QuestionnaireError />
            </CardContent>
          </QuestionnaireItem>
        ))}
        <CardFooter>
          <QuestionnaireActions>
            <QuestionnairePrevious size="sm">이전</QuestionnairePrevious>
            <QuestionnaireSkip size="sm">건너뛰기</QuestionnaireSkip>
            <QuestionnaireNext size="sm">다음</QuestionnaireNext>
            <QuestionnaireSubmit size="sm" disabled={submitted}>
              {submitted ? "전달 중…" : "답하기"}
            </QuestionnaireSubmit>
          </QuestionnaireActions>
        </CardFooter>
      </Card>
    </Questionnaire>
  );
}
