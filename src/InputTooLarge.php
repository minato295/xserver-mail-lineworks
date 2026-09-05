<?php
declare(strict_types=1);
namespace XserverMail;

/** Fixed reason only: never carries received content. */
final class InputTooLarge extends \RuntimeException {}
