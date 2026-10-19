import { useState, useRef, useEffect, useCallback } from 'react';

interface TooltipProps {
  content: React.ReactNode;
  children: React.ReactNode;
  delay?: number;
  className?: string;
}

export function Tooltip({ content, children, delay = 300, className = '' }: TooltipProps) {
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const timeoutRef = useRef<NodeJS.Timeout | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const targetRectRef = useRef<DOMRect | null>(null);

  const updatePosition = useCallback(() => {
    if (!tooltipRef.current || !targetRectRef.current) return;
    
    const tooltipWidth = tooltipRef.current.offsetWidth;
    const tooltipHeight = tooltipRef.current.offsetHeight;
    const rect = targetRectRef.current;
    
    const windowWidth = window.innerWidth;
    const windowHeight = window.innerHeight;
    
    let top = rect.top + (rect.height / 2) - (tooltipHeight / 2);
    let left = rect.right + 8;
    
    if (left + tooltipWidth > windowWidth - 10) {
      left = rect.left - tooltipWidth - 8;
    }
    
    if (left < 10) {
      left = 10;
    }
    
    if (top < 10) {
      top = 10;
    } else if (top + tooltipHeight > windowHeight - 10) {
      top = windowHeight - tooltipHeight - 10;
    }
    
    setPosition({ top, left });
  }, []);

  const handleMouseEnter = (e: React.MouseEvent) => {
    targetRectRef.current = e.currentTarget.getBoundingClientRect();
    timeoutRef.current = setTimeout(() => {
      setIsVisible(true);
    }, delay);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    setIsVisible(false);
    targetRectRef.current = null;
  };

  useEffect(() => {
    if (isVisible) {
      updatePosition();
    }
  }, [isVisible, updatePosition]);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, []);

  return (
    <div
      ref={containerRef}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      className={className}
      style={{ display: 'inline-block' }}
    >
      {children}
      {isVisible && (
        <div
          ref={tooltipRef}
          className="ucd-tooltip"
          style={{
            position: 'fixed',
            top: `${position.top}px`,
            left: `${position.left}px`,
            zIndex: 10000,
          }}
        >
          {content}
        </div>
      )}
    </div>
  );
}
