import type { StepIdx } from '../types';

type StepDef = { n: number; label: string };

const STEPS: StepDef[] = [
  { n: 1, label: '手机号 · 支付' },
  { n: 2, label: '飞书凭证' },
  { n: 3, label: '配对码' },
];

type Props = {
  currentStep: StepIdx;
};

export function ProgressBar({ currentStep }: Props) {
  return (
    <div className="progress-bar">
      {STEPS.map((s, i) => {
        const isCompleted = s.n < currentStep;
        const isActive = s.n === currentStep;
        return (
          <div
            key={s.n}
            className={`progress-step ${isCompleted ? 'completed' : ''} ${isActive ? 'active' : ''}`}
          >
            <span className="progress-dot">
              {isCompleted ? '\u2713' : s.n}
            </span>
            <span className="progress-label">{s.label}</span>
            {i < STEPS.length - 1 ? <span className="progress-line" /> : null}
          </div>
        );
      })}
    </div>
  );
}
