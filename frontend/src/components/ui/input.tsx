import * as React from "react"

import { cn } from "@/lib/utils"

function Input({ className, type, id, ...props }: React.ComponentProps<"input">) {
  // Un id estable por instancia satisface el requisito de Chrome ("a form field element should have
  // an id or name attribute") cuando el consumidor no pasa id ni name; si pasa id, se respeta.
  const reactId = React.useId()
  return (
    <input
      type={type}
      id={id ?? reactId}
      data-slot="input"
      className={cn(
        "h-9 w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-1 text-base shadow-xs transition-[color,box-shadow] outline-none selection:bg-primary selection:text-primary-foreground file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:bg-input/30",
        "focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
        "aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40",
        className
      )}
      {...props}
    />
  )
}

export { Input }
