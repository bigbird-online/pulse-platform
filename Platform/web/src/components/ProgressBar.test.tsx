import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ProgressBar } from './ProgressBar';

describe('ProgressBar', () => {
  it('renders 3 step dots', () => {
    render(<ProgressBar currentStep={1} />);
    const dots = screen.getAllByText(/\d/);
    expect(dots).toHaveLength(3);
  });

  it('marks current step as active', () => {
    render(<ProgressBar currentStep={2} />);
    const step2 = screen.getByText('2');
    expect(step2.closest('.progress-step')).toHaveClass('active');
  });

  it('marks completed steps with checkmark', () => {
    render(<ProgressBar currentStep={3} />);
    // Step 1 should be completed (checkmark), Step 2 completed
    const checks = screen.getAllByText('\u2713');
    expect(checks).toHaveLength(2);
  });

  it('renders step labels', () => {
    render(<ProgressBar currentStep={1} />);
    expect(screen.getByText('手机号 · 支付')).toBeInTheDocument();
    expect(screen.getByText('飞书凭证')).toBeInTheDocument();
    expect(screen.getByText('配对码')).toBeInTheDocument();
  });
});
