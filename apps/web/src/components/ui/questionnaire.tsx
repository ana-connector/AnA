"use client"

/**
 * The stepped question form, owned by ANA.
 *
 * This used to be `@shadcn/react/questionnaire`. That primitive tracked its
 * answers by registering every rendered control in the DOM and reading them
 * back; this one keeps the answers in React state and renders real form
 * controls from it, which is enough for what `QuestionCard` asks of it and far
 * easier to reason about.
 *
 * What the card depends on, and what is therefore preserved exactly:
 *
 *   - one question visible at a time, the rest still in the form so a single
 *     `new FormData(form)` at submit holds every answer,
 *   - `q{index}` as the control name, one entry per selected choice, so a
 *     multiple-answer question yields several and a skipped one yields none,
 *   - required questions block Next and Submit and cannot be skipped,
 *   - number keys pick a choice, Enter moves on, arrows step between
 *     questions and between choices.
 */

import * as React from "react"
import { cn } from "cn"

import { buttonVariants, type Button } from "@/components/ui/button"
import { CheckIcon } from "lucide-react"

type ItemStatus = "unanswered" | "answered" | "skipped"
type ShortcutMode = "letters" | "numbers"

type ItemDefinition = {
  name: string
  required?: boolean
  disabled?: boolean
}

/** What one question holds while it is being answered. */
type Answer = {
  choices: string[]
  text: string
  skipped: boolean
  /** Set once the reader has tried to leave the question, so an empty required
   *  answer only complains after they have had a go at it. */
  touched: boolean
}

type RegisteredItem = {
  name: string
  required: boolean
  multiple: boolean
}

const EMPTY: Answer = { choices: [], text: "", skipped: false, touched: false }

function answered(answer: Answer): boolean {
  return answer.choices.length > 0 || answer.text.trim().length > 0
}

function statusOf(answer: Answer): ItemStatus {
  if (answer.skipped) return "skipped"
  return answered(answer) ? "answered" : "unanswered"
}

function shortcutKeys(mode: ShortcutMode | null): string[] {
  if (mode === "letters") return Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i))
  if (mode === "numbers") return Array.from({ length: 9 }, (_, i) => String(i + 1))
  return []
}

type RootContext = {
  activeName: string | null
  answers: Record<string, Answer>
  current: number
  first: boolean
  last: boolean
  total: number
  shortcuts: ShortcutMode | null
  order: string[]
  register: (item: RegisteredItem) => () => void
  itemsByName: Map<string, RegisteredItem>
  update: (name: string, change: (answer: Answer) => Answer) => void
  goTo: (name: string) => void
  goPrevious: () => void
  goNext: () => void
  skipCurrent: () => void
  submitNow: () => void
}

type ItemContext = {
  name: string
  multiple: boolean
  required: boolean
  active: boolean
  invalid: boolean
  status: ItemStatus
  answer: Answer
  shortcuts: ShortcutMode | null
  toggleChoice: (value: string, checked: boolean) => void
  setText: (text: string) => void
}

const RootContextObject = React.createContext<RootContext | null>(null)
const ItemContextObject = React.createContext<ItemContext | null>(null)

function useRoot(component: string): RootContext {
  const context = React.useContext(RootContextObject)
  if (!context) throw new Error(`${component} must be used within a Questionnaire.`)
  return context
}

function useItem(component: string): ItemContext {
  const context = React.useContext(ItemContextObject)
  if (!context) throw new Error(`${component} must be used within a QuestionnaireItem.`)
  return context
}

function isTextEntry(target: EventTarget | null): boolean {
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true
  if (target instanceof HTMLInputElement) {
    return !["button", "checkbox", "radio", "reset", "submit"].includes(target.type)
  }
  return target instanceof HTMLElement && target.isContentEditable
}

function Questionnaire({
  children,
  className,
  defaultItem,
  items,
  onSubmit,
  shortcuts,
  ...props
}: Omit<React.ComponentProps<"form">, "defaultValue"> & {
  defaultItem?: string
  items?: readonly ItemDefinition[]
  shortcuts?: ShortcutMode
}) {
  const formRef = React.useRef<HTMLFormElement | null>(null)
  const [registered, setRegistered] = React.useState<RegisteredItem[]>([])
  const [answers, setAnswers] = React.useState<Record<string, Answer>>({})
  const [chosenName, setActiveName] = React.useState<string | null>(defaultItem ?? null)

  const register = React.useCallback((item: RegisteredItem) => {
    setRegistered((current) => [...current.filter((other) => other.name !== item.name), item])
    return () => setRegistered((current) => current.filter((other) => other.name !== item.name))
  }, [])

  const itemsByName = React.useMemo(
    () => new Map(registered.map((item) => [item.name, item])),
    [registered]
  )

  // `items` gives the order and which questions count; a question that is
  // declared but not yet rendered still holds its place in the progress.
  const order = React.useMemo(() => {
    if (items) return items.filter((item) => !item.disabled).map((item) => item.name)
    return registered.map((item) => item.name)
  }, [items, registered])

  // Derived rather than synchronised: the first question is active until one
  // is chosen, and a chosen question that leaves the set hands it back.
  const chosenIndex = chosenName ? order.indexOf(chosenName) : -1
  const index = chosenIndex >= 0 ? chosenIndex : order.length > 0 ? 0 : -1
  const activeName = index < 0 ? null : order[index]

  const answerFor = React.useCallback(
    (name: string) => answers[name] ?? EMPTY,
    [answers]
  )

  const update = React.useCallback((name: string, change: (answer: Answer) => Answer) => {
    setAnswers((current) => ({ ...current, [name]: change(current[name] ?? EMPTY) }))
  }, [])

  const requiredOf = React.useCallback(
    (name: string) => {
      const rendered = itemsByName.get(name)
      if (rendered) return rendered.required
      return Boolean(items?.find((item) => item.name === name)?.required)
    },
    [items, itemsByName]
  )

  /** True when the question may be left. Marks it touched either way. */
  const validate = React.useCallback(
    (name: string) => {
      const answer = answerFor(name)
      update(name, (current) => (current.touched ? current : { ...current, touched: true }))
      if (answer.skipped && !requiredOf(name)) return true
      return answered(answer)
    },
    [answerFor, requiredOf, update]
  )

  // Focus has to follow the question, or the keyboard is stranded: the
  // control it was on goes `hidden` with the question it belonged to, the
  // browser drops focus to the body, and the form stops hearing keys.
  const focusWanted = React.useRef(false)
  const goTo = React.useCallback((name: string) => {
    focusWanted.current = true
    setActiveName(name)
  }, [])

  const goPrevious = React.useCallback(() => {
    if (index <= 0) return
    goTo(order[index - 1])
  }, [goTo, index, order])

  const goNext = React.useCallback(() => {
    if (index < 0 || index >= order.length - 1) return
    if (!validate(order[index])) return
    goTo(order[index + 1])
  }, [goTo, index, order, validate])

  const submitNow = React.useCallback(() => {
    if (index < 0) return
    if (!validate(order[index])) return
    if (index === order.length - 1) formRef.current?.requestSubmit()
    else goTo(order[index + 1])
  }, [goTo, index, order, validate])

  const skipCurrent = React.useCallback(() => {
    if (index < 0) return
    const name = order[index]
    if (requiredOf(name)) return
    update(name, (current) => ({ ...current, choices: [], text: "", skipped: true, touched: false }))
    if (index < order.length - 1) goTo(order[index + 1])
    // The skip has to be in the state the form reads, so the submit waits a
    // tick: the skipped question's controls lose their name on the next render.
    else queueMicrotask(() => formRef.current?.requestSubmit())
  }, [goTo, index, order, requiredOf, update])

  React.useEffect(() => {
    if (!focusWanted.current) return
    focusWanted.current = false
    formRef.current
      ?.querySelector<HTMLElement>('[data-slot="questionnaire-item"]:not([hidden])')
      ?.focus()
  }, [activeName])

  const handleSubmit = (event: React.SubmitEvent<HTMLFormElement>) => {
    const unanswered = order.find((name) => {
      const answer = answerFor(name)
      if (answer.skipped && !requiredOf(name)) return false
      return !answered(answer)
    })
    if (unanswered) {
      event.preventDefault()
      setAnswers((current) => {
        const next = { ...current }
        for (const name of order) next[name] = { ...(next[name] ?? EMPTY), touched: true }
        return next
      })
      goTo(unanswered)
      return
    }
    onSubmit?.(event)
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLFormElement>) => {
    if (event.defaultPrevented || event.nativeEvent.isComposing || event.keyCode === 229) return
    if (activeName === null) return
    const item = itemsByName.get(activeName)

    if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey) {
      event.preventDefault()
      if (!event.repeat) submitNow()
      return
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return

    if ((event.key === "ArrowUp" || event.key === "ArrowDown") && moveChoiceFocus(event)) {
      event.preventDefault()
      return
    }

    if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && !isTextEntry(event.target)) {
      event.preventDefault()
      if (event.repeat) return
      if (event.key === "ArrowLeft") goPrevious()
      else if (statusOf(answerFor(activeName)) !== "unanswered") goNext()
      return
    }

    if (event.key === "Enter" && event.target instanceof HTMLInputElement
      && ["radio", "checkbox"].includes(event.target.type)) {
      event.preventDefault()
      if (!event.repeat && event.target.checked) submitNow()
      return
    }

    if (!shortcuts || isTextEntry(event.target) || !item) return
    const keys = shortcutKeys(shortcuts)
    const pressed = shortcuts === "letters" ? event.key.toUpperCase() : event.key
    const position = keys.indexOf(pressed)
    if (position < 0) return
    const choices = Array.from(
      event.currentTarget.querySelectorAll<HTMLInputElement>(
        `input[data-slot="questionnaire-choice-input"][data-item="${CSS.escape(activeName)}"]`
      )
    )
    const choice = choices[position]
    if (!choice) return
    event.preventDefault()
    if (event.repeat) return
    choice.focus()
    choice.click()
  }

  const value = React.useMemo<RootContext>(
    () => ({
      activeName,
      answers,
      current: index < 0 ? 0 : index + 1,
      first: order.length > 0 && index === 0,
      last: order.length > 0 && index === order.length - 1,
      total: order.length,
      shortcuts: shortcuts ?? null,
      order,
      register,
      itemsByName,
      update,
      goTo,
      goPrevious,
      goNext,
      skipCurrent,
      submitNow,
    }),
    [
      activeName, answers, index, order, shortcuts, register, itemsByName, update, goTo,
      goPrevious, goNext, skipCurrent, submitNow,
    ]
  )

  return (
    <RootContextObject.Provider value={value}>
      <form
        ref={formRef}
        data-slot="questionnaire"
        data-shortcuts={shortcuts ?? undefined}
        noValidate
        onKeyDown={handleKeyDown}
        onSubmit={handleSubmit}
        className={cn("flex w-full min-w-0 flex-col gap-4", className)}
        {...props}
      >
        {children}
      </form>
    </RootContextObject.Provider>
  )
}

/** Up and down walk the current question's choices, wrapping at the ends. */
function moveChoiceFocus(event: React.KeyboardEvent<HTMLFormElement>): boolean {
  const target = event.target
  if (target instanceof HTMLFieldSetElement) {
    // Focus is on the question itself, right after moving to it: step into
    // its choices rather than doing nothing.
    const choices = target.querySelectorAll<HTMLInputElement>(
      'input[data-slot="questionnaire-choice-input"]'
    )
    if (choices.length === 0) return false
    choices[event.key === "ArrowDown" ? 0 : choices.length - 1].focus()
    return true
  }
  if (!(target instanceof HTMLInputElement) || !["radio", "checkbox"].includes(target.type)) {
    return false
  }
  // A radio group already does this itself, and taking it over would skip the
  // selection that comes with it.
  if (target.type === "radio") return false
  const group = Array.from(
    event.currentTarget.querySelectorAll<HTMLInputElement>(
      `input[data-slot="questionnaire-choice-input"][data-item="${CSS.escape(target.name)}"]`
    )
  )
  const at = group.indexOf(target)
  if (at < 0 || group.length < 2) return false
  const step = event.key === "ArrowDown" ? 1 : -1
  group[(at + step + group.length) % group.length].focus()
  return true
}

function QuestionnaireProgress({ className, children, ...props }: React.ComponentProps<"div">) {
  const { current, total } = useRoot("QuestionnaireProgress")
  // Korean, like everything else the person reads here.
  const label = total ? `질문 ${current} / ${total}` : undefined
  return (
    <div
      data-slot="questionnaire-progress"
      role="progressbar"
      aria-label="질문 진행"
      aria-live="polite"
      aria-valuemin={total ? 1 : undefined}
      aria-valuemax={total || undefined}
      aria-valuenow={total ? current : undefined}
      aria-valuetext={label}
      className={cn(
        "min-h-[1lh] w-fit min-w-[14ch] text-xs font-medium text-muted-foreground tabular-nums",
        className
      )}
      {...props}
    >
      {children ?? label}
    </div>
  )
}

function QuestionnaireItem({
  className,
  children,
  multiple = false,
  name,
  required = false,
  ...props
}: Omit<React.ComponentProps<"fieldset">, "name"> & {
  name: string
  multiple?: boolean
  required?: boolean
}) {
  const root = useRoot("QuestionnaireItem")
  const { register, update } = root
  const answer = root.answers[name] ?? EMPTY
  const active = root.activeName === name
  const status = statusOf(answer)
  const invalid = answer.touched && status === "unanswered"

  React.useEffect(() => register({ name, required, multiple }), [multiple, name, register, required])

  const toggleChoice = React.useCallback(
    (value: string, checked: boolean) => {
      update(name, (current) => {
        const choices = multiple
          ? checked
            ? [...current.choices.filter((other) => other !== value), value]
            : current.choices.filter((other) => other !== value)
          : checked
            ? [value]
            : []
        // One answer per question unless it takes several: picking a choice
        // retires whatever was typed, and typing retires the choice.
        return { ...current, choices, text: multiple ? current.text : "", skipped: false }
      })
    },
    [multiple, name, update]
  )

  const setText = React.useCallback(
    (text: string) => {
      update(name, (current) => ({
        ...current,
        text,
        choices: multiple || text.trim().length === 0 ? current.choices : [],
        skipped: false,
      }))
    },
    [multiple, name, update]
  )

  const value = React.useMemo<ItemContext>(
    () => ({
      name, multiple, required, active, invalid, status, answer,
      shortcuts: root.shortcuts, toggleChoice, setText,
    }),
    [active, answer, invalid, multiple, name, required, root.shortcuts, setText, status,
     toggleChoice]
  )

  return (
    <ItemContextObject.Provider value={value}>
      <fieldset
        data-slot="questionnaire-item"
        data-active={active ? "" : undefined}
        aria-invalid={invalid || undefined}
        hidden={!active}
        inert={!active}
        tabIndex={-1}
        className={cn("flex min-w-0 flex-col gap-4 border-0 p-0 outline-none", className)}
        {...props}
      >
        {children}
      </fieldset>
    </ItemContextObject.Provider>
  )
}

/** Renders as `render`'s element when one is given, the way the primitive did,
 *  so a title can be a `CardTitle` without an extra wrapper. */
function withRender(
  render: React.ReactElement<Record<string, unknown>> | undefined,
  fallback: React.ElementType,
  props: Record<string, unknown>
) {
  if (render) {
    return React.cloneElement(render, {
      ...props,
      ...render.props,
      className: cn(props.className as string, render.props.className as string),
    })
  }
  const Tag = fallback
  return <Tag {...props} />
}

function QuestionnaireTitle({
  className,
  render,
  ...props
}: React.ComponentProps<"legend"> & { render?: React.ReactElement<Record<string, unknown>> }) {
  useItem("QuestionnaireTitle")
  return withRender(render, "legend", {
    "data-slot": "questionnaire-title",
    className: cn(
      "font-heading text-base leading-snug font-medium text-pretty [&:not(:has(~[data-slot=questionnaire-description]))]:mb-4",
      className
    ),
    ...props,
  })
}

function QuestionnaireDescription({
  className,
  render,
  ...props
}: React.ComponentProps<"p"> & { render?: React.ReactElement<Record<string, unknown>> }) {
  useItem("QuestionnaireDescription")
  return withRender(render, "p", {
    "data-slot": "questionnaire-description",
    className: cn("text-sm text-pretty text-muted-foreground", className),
    ...props,
  })
}

function QuestionnaireChoices({ children, className, ...props }: React.ComponentProps<"div">) {
  const { shortcuts } = useRoot("QuestionnaireChoices")
  // Which key picks which choice is decided here, by position among the
  // choices rendered. A choice cannot work it out for itself without writing
  // to something shared mid-render, which server and client disagree about.
  let position = 0
  const numbered = React.Children.map(children, (child) => {
    if (!React.isValidElement(child) || child.type !== QuestionnaireChoice) return child
    const element = child as React.ReactElement<{ shortcutIndex?: number }>
    return React.cloneElement(element, { shortcutIndex: position++ })
  })
  return (
    <div
      data-slot="questionnaire-choices"
      data-shortcuts={shortcuts ?? undefined}
      className={cn("group/questionnaire-choices grid min-w-0 gap-2", className)}
      {...props}
    >
      {numbered}
    </div>
  )
}

function QuestionnaireChoice({
  children,
  className,
  disabled = false,
  shortcutIndex,
  value,
  ...props
}: Omit<React.ComponentProps<"label">, "onChange"> & {
  value: string
  disabled?: boolean
  /** Set by QuestionnaireChoices; the position this choice's key comes from. */
  shortcutIndex?: number
}) {
  const item = useItem("QuestionnaireChoice")
  const checked = item.answer.choices.includes(value)
  const type = item.multiple ? "checkbox" : "radio"
  const shortcut =
    shortcutIndex === undefined ? null : shortcutKeys(item.shortcuts)[shortcutIndex] ?? null

  return (
    <label
      data-slot="questionnaire-choice"
      data-type={type}
      data-checked={checked ? "" : undefined}
      data-unchecked={checked ? undefined : ""}
      data-disabled={disabled ? "" : undefined}
      data-shortcut={shortcut ?? undefined}
      className={cn(
        "group/questionnaire-choice relative flex min-h-11 cursor-pointer items-start gap-2.5 rounded-lg border border-input bg-transparent px-3 py-2.5 text-start text-sm transition-colors outline-none select-none hover:bg-muted/50 has-[>input:focus-visible]:border-ring has-[>input:focus-visible]:ring-3 has-[>input:focus-visible]:ring-ring/50 data-invalid:border-destructive dark:bg-input/20 data-checked:border-primary/40 data-checked:bg-muted dark:data-checked:bg-muted",
        "data-disabled:pointer-events-none data-disabled:cursor-not-allowed data-disabled:opacity-50",
        className
      )}
      {...props}
    >
      <input
        data-slot="questionnaire-choice-input"
        data-item={item.name}
        type={type}
        // A skipped question contributes nothing to the submitted form, which
        // is how an unanswered optional question reaches the server as "".
        name={item.answer.skipped ? undefined : item.name}
        value={value}
        checked={checked}
        disabled={disabled}
        aria-invalid={item.invalid || undefined}
        aria-keyshortcuts={shortcut ?? undefined}
        onChange={(event) => item.toggleChoice(value, event.target.checked)}
        className="absolute inset-0 z-10 size-full cursor-pointer opacity-0"
      />
      <span
        aria-hidden="true"
        data-slot="questionnaire-choice-indicator"
        className="pointer-events-none relative flex size-4 shrink-0 translate-y-[--spacing(0.45)] items-center justify-center rounded-[4px] border border-input group-has-data-[slot=questionnaire-choice-description]/questionnaire-choice:translate-y-0.5 group-data-[type=radio]/questionnaire-choice:rounded-full group-data-checked/questionnaire-choice:border-primary group-data-checked/questionnaire-choice:bg-primary group-data-checked/questionnaire-choice:text-primary-foreground dark:bg-input/30 dark:group-data-checked/questionnaire-choice:bg-primary"
      >
        <span
          data-slot="questionnaire-choice-indicator-dot"
          className="hidden size-2 rounded-full bg-primary-foreground group-data-[type=checkbox]/questionnaire-choice:hidden group-data-checked/questionnaire-choice:block"
        />
        <CheckIcon
          data-slot="questionnaire-choice-indicator-check"
          className="hidden size-3.5 group-data-[type=radio]/questionnaire-choice:hidden group-data-checked/questionnaire-choice:block"
        />
      </span>
      <span
        data-slot="questionnaire-choice-label"
        className="flex min-w-0 flex-1 flex-col gap-0.5 leading-snug"
      >
        {children}
      </span>
      <span
        aria-hidden="true"
        data-slot="questionnaire-choice-shortcut"
        hidden={shortcut === null}
        className="pointer-events-none ms-auto hidden size-5 shrink-0 translate-y-[--spacing(0.45)] items-center justify-center rounded-md border border-input bg-background font-mono text-[0.625rem] leading-none font-medium text-muted-foreground group-has-data-[slot=questionnaire-choice-description]/questionnaire-choice:translate-y-0.5 group-data-[shortcut]/questionnaire-choice:inline-flex"
      >
        {shortcut}
      </span>
    </label>
  )
}

function QuestionnaireChoiceDescription({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="questionnaire-choice-description"
      className={cn("text-muted-foreground", className)}
      {...props}
    />
  )
}

function QuestionnaireInput({
  className,
  disabled = false,
  type = "text",
  ...props
}: Omit<React.ComponentProps<"input">, "name" | "type" | "value"> & { type?: string }) {
  const item = useItem("QuestionnaireInput")
  const filled = item.answer.text.trim().length > 0
  return (
    <div
      data-slot="questionnaire-input-wrapper"
      className="group/questionnaire-input relative w-full min-w-0"
    >
      <input
        data-slot="questionnaire-input"
        data-filled={filled ? "" : undefined}
        data-empty={filled ? undefined : ""}
        type={type}
        // Only a filled, unskipped free answer is part of the submission; an
        // empty box must not add a "" to the question's answers.
        name={filled && !item.answer.skipped ? item.name : undefined}
        value={item.answer.text}
        disabled={disabled}
        aria-invalid={item.invalid || undefined}
        onChange={(event) => item.setText(event.target.value)}
        className={cn(
          "h-8 min-h-11 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-[color,box-shadow,background-color] outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 sm:min-h-0 md:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
          "selection:bg-primary selection:text-primary-foreground placeholder:text-muted-foreground",
          className
        )}
        {...props}
      />
    </div>
  )
}

function QuestionnaireError({ children, className, ...props }: React.ComponentProps<"p">) {
  const item = useItem("QuestionnaireError")
  return (
    <p
      data-slot="questionnaire-error"
      role={item.invalid ? "alert" : undefined}
      hidden={!item.invalid}
      className={cn("mt-2 text-sm text-destructive", className)}
      {...props}
    >
      {children ??
        (item.required ? "답을 골라야 다음으로 넘어갑니다." : "답을 고르거나 이 질문을 건너뛰세요.")}
    </p>
  )
}

function QuestionnaireActions({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="questionnaire-actions"
      className={cn(
        "grid min-h-11 w-full grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2 sm:min-h-8",
        className
      )}
      {...props}
    />
  )
}

type ActionProps = React.ComponentProps<"button"> &
  Pick<React.ComponentProps<typeof Button>, "size" | "variant">

/** The four navigation buttons differ only in when they are there and what
 *  they do, so they share one body: hidden means hidden from the keyboard and
 *  from assistive technology too, not merely invisible. */
function NavigationButton({
  children,
  className,
  column,
  disabled = false,
  onClick,
  shortcut,
  size = "default",
  slot,
  tabIndex,
  type = "button",
  variant = "outline",
  visible,
  ...props
}: ActionProps & {
  column: 1 | 2 | 3
  shortcut?: "Enter"
  slot: string
  visible: boolean
}) {
  return (
    <button
      data-slot={slot}
      data-size={size}
      data-variant={variant}
      data-visible={visible ? "" : undefined}
      data-hidden={visible ? undefined : ""}
      aria-hidden={!visible || undefined}
      aria-keyshortcuts={visible && !disabled ? shortcut : undefined}
      disabled={disabled}
      hidden={!visible}
      inert={!visible}
      tabIndex={visible ? tabIndex : -1}
      type={type}
      onClick={onClick}
      className={cn(
        buttonVariants({ size, variant }),
        "row-start-1 min-h-11 sm:min-h-0",
        column === 1 && "col-start-1 justify-self-start",
        column === 2 && "col-start-2 justify-self-end",
        column === 3 && "col-start-3 justify-self-end",
        className
      )}
      {...props}
    >
      {children}
    </button>
  )
}

function QuestionnairePrevious({ children, onClick, ...props }: ActionProps) {
  const root = useRoot("QuestionnairePrevious")
  return (
    <NavigationButton
      column={1}
      slot="questionnaire-previous"
      visible={root.total > 1 && !root.first}
      onClick={(event) => {
        onClick?.(event)
        if (!event.defaultPrevented) root.goPrevious()
      }}
      {...props}
    >
      {children ?? "Previous"}
    </NavigationButton>
  )
}

function QuestionnaireSkip({ children, onClick, ...props }: ActionProps) {
  const root = useRoot("QuestionnaireSkip")
  const active = root.activeName ? root.itemsByName.get(root.activeName) : undefined
  return (
    <NavigationButton
      column={2}
      slot="questionnaire-skip"
      visible={active !== undefined && !active.required}
      onClick={(event) => {
        onClick?.(event)
        if (!event.defaultPrevented) root.skipCurrent()
      }}
      {...props}
    >
      {children ?? "Skip"}
    </NavigationButton>
  )
}

function QuestionnaireNext({ children, onClick, variant = "default", ...props }: ActionProps) {
  const root = useRoot("QuestionnaireNext")
  return (
    <NavigationButton
      column={3}
      shortcut="Enter"
      slot="questionnaire-next"
      variant={variant}
      visible={root.total > 1 && !root.last}
      onClick={(event) => {
        onClick?.(event)
        if (!event.defaultPrevented) root.goNext()
      }}
      {...props}
    >
      {children ?? "Next"}
    </NavigationButton>
  )
}

function QuestionnaireSubmit({
  children,
  type = "submit",
  variant = "default",
  ...props
}: ActionProps) {
  const root = useRoot("QuestionnaireSubmit")
  return (
    <NavigationButton
      column={3}
      shortcut="Enter"
      slot="questionnaire-submit"
      type={type}
      variant={variant}
      visible={root.total > 0 && root.last}
      {...props}
    >
      {children ?? "Submit"}
    </NavigationButton>
  )
}

export {
  Questionnaire,
  QuestionnaireActions,
  QuestionnaireChoice,
  QuestionnaireChoiceDescription,
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
}
